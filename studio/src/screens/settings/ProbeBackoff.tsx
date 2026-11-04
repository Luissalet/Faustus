import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components';
import { clearProbeBackoff, getProbeBackoff, type ProbeBackoffStatus } from '../../adapters/probeBackoff';
import { SETTINGS_SAVED_EVENT } from '../../adapters/settings';
import { t } from '../../i18n';

interface NamedEndpoint { id: string; name?: string | null }

/**
 * Settings > Models: model servers whose capability probes are paused after
 * refused connections, with the endpoints that point at each one, when the
 * next probe goes out, and a button to probe again now.
 */
export function ProbeBackoffSection({ endpoints = [], say }: { endpoints?: NamedEndpoint[]; say?: (msg: string) => void }) {
  const [s, setS] = useState<ProbeBackoffStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => getProbeBackoff()
    .then((d) => { setS(d); setErr(null); })
    .catch((e: Error) => setErr(e.message)), []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    window.addEventListener(SETTINGS_SAVED_EVENT, load);
    return () => { window.clearInterval(timer); window.removeEventListener(SETTINGS_SAVED_EVENT, load); };
  }, [load]);

  const retry = async (server?: string) => {
    setBusy(server ?? '*');
    try {
      const n = await clearProbeBackoff(server ? { url: server } : {});
      say?.(t('Probes resumed for {n} server(s)', { n }));
      await load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const nameOf = (id: string) => s?.endpoint_names?.[id] || endpoints.find((e) => e.id === id)?.name || id;
  const endpointsFor = (server: string) => Object.entries(s?.endpoints ?? {})
    .filter(([, srv]) => srv === server).map(([id]) => nameOf(id));

  return (
    <div className="fs-set__card" data-testid="probe-backoff-card">
      <h3 className="fs-set__card-title">{t('Paused model probes')}</h3>
      <p className="fs-set__help">
        {t('A model server that refuses {n} connections in a row is skipped by automatic detection (vision helper, context window, model capabilities, hedge targets) for {base} s, doubling on each further refusal up to {max} min. Any answer from it ends the pause. The endpoints stay as they are.', {
          n: s?.threshold ?? 2, base: s?.base_s ?? 60, max: Math.round((s?.max_s ?? 900) / 60),
        })}
      </p>
      {err && <p className="fs-set__err" role="alert">{err}</p>}
      {!s && !err && <p className="fs-set__help">{t('Loading')}</p>}
      {s && s.servers.length === 0 && (
        <p className="fs-set__help" data-testid="probe-backoff-empty">
          {t('No server is paused: every model server answered its last probe.')}
        </p>
      )}
      {s && s.servers.length > 0 && (
        <>
          <table className="fs-autonomy-table" data-testid="probe-backoff-table">
            <thead>
              <tr>
                <th>{t('Server')}</th>
                <th>{t('Endpoints')}</th>
                <th>{t('Refusals')}</th>
                <th>{t('Next probe')}</th>
                <th>{t('Last error')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {s.servers.map((row) => (
                <tr key={row.server} data-server={row.server}>
                  <td><code>{row.server}</code></td>
                  <td>{endpointsFor(row.server).join(', ') || '-'}</td>
                  <td>{row.failures}</td>
                  <td>{row.skipped ? t('in {s} s', { s: Math.ceil(row.retry_in_s) }) : t('next use')}</td>
                  <td title={row.last_error}>{row.last_error.slice(0, 80)}</td>
                  <td>
                    <Button variant="ghost" size="sm" label={t('Probe now')} disabled={busy !== null}
                      loading={busy === row.server} testId={`probe-backoff-retry-${row.server}`}
                      onClick={() => void retry(row.server)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Button variant="secondary" size="sm" label={t('Probe all now')} disabled={busy !== null}
            loading={busy === '*'} testId="probe-backoff-retry-all" onClick={() => void retry()} />
        </>
      )}
    </div>
  );
}
