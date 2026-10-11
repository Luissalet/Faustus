import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { ApiError, getJson } from '../adapters/api';
import { relativeTime } from '../adapters/home';
import { t } from '../i18n';
import './fleet.css';

interface FleetItem {
  id: string; label: string; state: string; reason: string; kind: string; observed_ts?: number;
  last_success_ts?: number | null; last_error_ts?: number | null; needs_user?: boolean; user_reason?: string | null; timestamp_basis?: string;
}
interface FleetResult {
  ok: boolean; generated_ts: number; items: FleetItem[];
  brief: Record<string, FleetItem[]>; refreshing?: boolean;
  coverage?: { errors?: Record<string, string>; truncated?: boolean; os_tasks?: string };
}

export function FleetCard() {
  const [data, setData] = useState<FleetResult | null>(null);
  const [busy, setBusy] = useState(true);
  const [failed, setFailed] = useState(false);
  const [denied, setDenied] = useState(false);
  const [revision, setRevision] = useState(0);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const control = new AbortController();
    let epoch = 0;
    const load = async () => {
      const ticket = ++epoch;
      setBusy(true);
      try {
        const result = await getJson<FleetResult>('/api/hoard/fleet', control.signal);
        if (control.signal.aborted || ticket !== epoch) return;
        setData(result.ok ? result : null); setFailed(!result.ok); setDenied(false);
      } catch (error) {
        if (!control.signal.aborted && ticket === epoch) { setData(null); setFailed(true); setDenied(error instanceof ApiError && error.status === 403); }
      } finally { if (!control.signal.aborted && ticket === epoch) setBusy(false); }
    };
    void load();
    const interval = window.setInterval(() => { void load(); }, 60000);
    return () => { control.abort(); epoch++; window.clearInterval(interval); };
  }, [revision]);
  const labels: Record<string, string> = { healthy: t('Working'), silent: t('Stopped silently'), error: t('Stopped with error'), paused: t('Paused'), unknown: t('Unverified') };
  const reasons: Record<string, string> = { login: t('Sign-in needed'), approval: t('Approval needed'), configuration: t('Configuration needed'), other: t('Needs your attention') };
  const groups = [['waiting_for_you', t('Waiting for you')], ['blocked', t('Blocked')], ['today', t('Today')], ['unknown', t('Unverified')], ['paused', t('Paused')]];
  return <section className="fs-block fs-fleet" aria-labelledby="fleet-title" aria-busy={busy}>
    <div className="fs-block__head">
      <h2 className="fs-block__title" id="fleet-title">{t('Fleet')}</h2>
      <button type="button" className="fs-fleet-refresh" onClick={() => setRevision((v) => v + 1)} disabled={busy} aria-label={t('Refresh fleet')}>
        <RefreshCw size={14} aria-hidden="true" />{t('Refresh')}
      </button>
    </div>
    <div aria-live="polite">
      {busy && !data && !failed && <p className="fs-fleet-note">{t('Checking fleet…')}</p>}
      {failed && <p className="fs-fleet-note">{denied ? t('An administrator can view the fleet for this device.') : t('Fleet could not be checked. Check the Hub and try again.')}</p>}
      {data && <>
        {data.refreshing && <p className="fs-fleet-note">{t('Updating fleet…')}</p>}
        {data.items.length === 0 && !data.refreshing && <p className="fs-fleet-note">{t('No fleet expectations are declared yet.')}</p>}
        {groups.map(([key, title]) => {
          const rows = data.brief[key] || [];
          if (!rows.length) return null;
          return <div className="fs-fleet-group" key={key}>
            <h3>{title} <span>{rows.length}</span></h3>
            <ul>{(expanded ? rows : rows.slice(0, 4)).map((row) => <li key={row.id}>
              <div><span className="fs-fleet-label">{row.label || row.id}</span><span className="fs-fleet-state">{row.reason === 'last_result_only' ? t('Last result succeeded') : labels[row.state] || t('Unverified')}</span></div>
              <p>{row.needs_user ? reasons[row.user_reason || 'other'] || t('Needs your attention')
                : row.kind === 'app' && row.state === 'healthy' && row.observed_ts ? t('Health checked: {when}', { when: relativeTime(row.observed_ts) })
                : row.kind === 'app' && row.state === 'silent' ? t('The app is not answering.')
                : row.state === 'error' && row.last_error_ts ? t('Last confirmed failure: {when}', { when: relativeTime(row.last_error_ts) })
                : row.last_success_ts && row.timestamp_basis?.includes('lower_bound') ? t('Successful run started: {when}', { when: relativeTime(row.last_success_ts) })
                : row.last_success_ts ? t('Last confirmed success: {when}', { when: relativeTime(row.last_success_ts) }) : t('No confirmed success recorded.')}</p>
            </li>)}</ul>
          </div>;
        })}
        {(data.brief.no_cadence?.length || 0) > 0 && <details className="fs-fleet-loose">
          <summary>{t('No schedule declared')} <span>{data.brief.no_cadence.length}</span></summary>
          <ul>{data.brief.no_cadence.map((row) => <li key={row.id}>
            <div><span className="fs-fleet-label">{row.label || row.id}</span></div>
            <p>{row.last_success_ts ? t('Last confirmed success: {when}', { when: relativeTime(row.last_success_ts) }) : t('No confirmed success recorded.')}</p>
          </li>)}</ul>
        </details>}
        {groups.some(([key]) => (data.brief[key]?.length || 0) > 4) && <button type="button" className="fs-fleet-refresh" onClick={() => setExpanded((value) => !value)}>{expanded ? t('Show fewer') : t('Show all')}</button>}
        {!!Object.keys(data.coverage?.errors || {}).length || data.coverage?.truncated ? <p className="fs-fleet-note">{t('Some fleet sources could not be checked.')}</p> : null}
        <p className="fs-fleet-note">{t('Only configured apps and schedules are checked. Other tasks appear when they report their results.')}</p>
      </>}
    </div>
  </section>;
}
