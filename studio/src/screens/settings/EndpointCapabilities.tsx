import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components';
import { loadProbes, runProbes, type CapabilityRow, type ProbeReport, type ProbeVerdict } from '../../adapters/modelProbes';
import type { ModelEndpoint } from '../../adapters/settings';
import { locale, t } from '../../i18n';

/**
 * What an OpenAI-compatible endpoint DECLARES versus what it was OBSERVED to do,
 * per model (Settings → Models). The two are never merged into one badge: a
 * declaration is an operator's word, a probe is a receipt for this endpoint as
 * configured now, and "the server said it does not" is its own verdict.
 */

const LABELS: Record<string, string> = {
  tool_calling: 'Tool calls',
  streaming_tool_calls: 'Streaming tool calls',
  json_mode: 'JSON mode',
  vision: 'Image input',
  context_length_effective: 'Effective context',
};

function verdictText(verdict: ProbeVerdict): string {
  switch (verdict) {
    case 'verified':
      return t('Works');
    case 'unsupported':
      return t('Not supported');
    case 'declared_unverified':
      return t('Declared, not checked');
    case 'declared_unsupported':
      return t('Declared unsupported, not checked');
    case 'undetermined':
      return t('Inconclusive');
    default:
      return t('Not checked');
  }
}

const declaredText = (v: boolean | null) => (v === true ? t('Yes') : v === false ? t('No') : '—');
const probedText = (v: boolean | null) => (v === true ? t('Passed') : v === false ? t('Failed') : '—');

function rowTitle(row: CapabilityRow): string {
  const when = row.tested_at ? new Date(row.tested_at).toLocaleString(locale()) : '';
  return [row.reason, row.status ? `HTTP ${row.status}` : '', when].filter(Boolean).join(' · ');
}

export function EndpointCapabilities({ endpoint, admin, say }: { endpoint: ModelEndpoint; admin: boolean; say: (text: string) => void }) {
  const [model, setModel] = useState(endpoint.models[0] ?? '');
  const [report, setReport] = useState<ProbeReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [probing, setProbing] = useState(false);
  const [withVision, setWithVision] = useState(false);
  const [note, setNote] = useState('');

  const load = useCallback(() => {
    if (!model) return;
    const controller = new AbortController();
    setLoading(true);
    setNote('');
    loadProbes(endpoint.id, model, controller.signal)
      .then(setReport)
      .catch((err: Error) => {
        if (controller.signal.aborted) return;
        setReport(null);
        setNote(err.message);
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [endpoint.id, model]);

  useEffect(() => load(), [load]);

  const probe = async () => {
    setProbing(true);
    setNote('');
    try {
      const probes = ['tool_calling', 'streaming_tool_calls', 'json_mode', ...(withVision ? ['vision'] : [])];
      setReport(await runProbes(endpoint.id, model, probes));
      say(t('Checked {name}', { name: model }));
    } catch (err) {
      setNote((err as Error).message);
    } finally {
      setProbing(false);
    }
  };

  if (endpoint.models.length === 0) return <p className="fs-set__help">{t('Reload the models first: there is nothing to check yet.')}</p>;

  const rows = (report?.capabilities ?? []).filter((r) => r.capability in LABELS);

  return (
    <div className="fs-epcap" data-testid={`ep-caps-${endpoint.id}`}>
      <div className="fs-epcap__bar">
        <label className="fs-epcap__pick">
          <span>{t('Model')}</span>
          <select value={model} onChange={(e) => setModel(e.target.value)} aria-label={t('Model')}>
            {endpoint.models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </label>
        {admin && (
          <>
            <label className="fs-epcap__opt">
              <input type="checkbox" checked={withVision} onChange={(e) => setWithVision(e.target.checked)} />
              <span>{t('Also send an image')}</span>
            </label>
            <Button
              variant="ghost"
              size="sm"
              label={probing ? t('Checking…') : t('Check what it does')}
              loading={probing}
              disabled={!model || probing}
              title={t('Sends a few short requests to this endpoint and records what it actually did')}
              onClick={() => void probe()}
            />
          </>
        )}
      </div>
      {note && <p className="fs-set__ep-error">{note}</p>}
      {loading && !report && <p className="fs-set__help">{t('Loading…')}</p>}
      {report && (
        <>
          <table className="fs-epcap__table">
            <thead>
              <tr>
                <th>{t('Capability')}</th>
                <th>{t('Declared')}</th>
                <th>{t('Observed')}</th>
                <th>{t('Verdict')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.capability} data-verdict={row.verdict} data-conflict={row.conflict || undefined}>
                  <td>{t(LABELS[row.capability] ?? row.capability)}</td>
                  <td>{declaredText(row.declared)}</td>
                  <td>{probedText(row.probed)}</td>
                  <td title={rowTitle(row)}>
                    <span className="fs-epcap__verdict" data-verdict={row.verdict}>{verdictText(row.verdict)}</span>
                    {row.conflict && <span className="fs-epcap__flag"> · {t('contradicts the declaration')}</span>}
                    {row.reason && row.verdict !== 'verified' && <span className="fs-epcap__why"> · {row.reason}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="fs-set__help">
            {report.saved
              ? t('Observed results belong to this endpoint as configured now. Changing its address, key or kind starts over.')
              : t('Nothing has been checked for this endpoint as configured now.')}
          </p>
        </>
      )}
    </div>
  );
}
