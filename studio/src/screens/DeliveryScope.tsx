import { GitCompareArrows } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState } from '../components';
import * as api from '../adapters/delivery';
import { t } from '../i18n';

/**
 * Scope changes (Hangar E30): the new request clause by clause against the
 * agreement. Effects are drawn only when the decision map grounds them; an
 * ungrounded effect says so in words instead of showing a guess.
 */

function StatusPill({ status }: { status: string }) {
  return <span className="fs-dlv__pill" data-tone={api.scopeTone(status)} data-status={status}>{api.scopeLabel(status)}</span>;
}

function Result({ cmp }: { cmp: api.ScopeComparison }) {
  return (
    <div className="fs-dlv__detail" data-testid="scope-result">
      <div className="fs-dlv__title-row">
        <h2 className="fs-dlv__title">{cmp.title || t('Scope comparison')}</h2>
        {(['included', 'changed', 'added', 'removed', 'ambiguous'] as api.ScopeStatus[]).map((s) => (
          cmp.counts[s] ? <span key={s} className="fs-dlv__pill" data-tone={api.scopeTone(s)}>{api.scopeLabel(s)} · {cmp.counts[s]}</span> : null
        ))}
      </div>
      <p className="fs-dlv__muted">{cmp.created} · {cmp.graph ? `${t('Decision map')}: ${cmp.graph}${cmp.graph_found ? '' : ` (${t('not found')})`}` : t('No decision map: effects are not grounded.')}</p>
      <section className="fs-dlv__block" aria-label={t('Clauses of the new request')}>
        <table className="fs-dlv__table">
          <thead><tr><th>{t('New request')}</th><th>{t('Status')}</th><th>{t('Agreed')}</th><th>{t('Effect')}</th><th>{t('Options')}</th></tr></thead>
          <tbody>
            {cmp.rows.map((r) => {
              const e = r.effect;
              const diff = r.diff?.attributes ? `${r.diff.attributes.from.join(', ')} → ${r.diff.attributes.to.join(', ')}`
                : r.diff?.quantities ? Object.entries(r.diff.quantities).map(([u, q]) => `${q.from} → ${q.to} ${u}`).join(' · ') : '';
              return (
                <tr key={r.n}>
                  <td>{r.clause}{diff && <div className="fs-dlv__muted">{diff}</div>}</td>
                  <td><StatusPill status={r.status} /></td>
                  <td className="fs-dlv__muted">{r.agreed_ref ? `${r.agreed_ref}: ${r.agreed}` : '—'}</td>
                  <td>
                    {e ? (
                      <>
                        <span className="fs-dlv__pill" data-tone={e.class === 'local' || e.class === 'none' ? 'good' : e.class === 'unknown' ? 'unknown' : 'warn'}>{api.effectLabel(e)}</span>
                        {e.recalculate && e.recalculate.length > 0 && <div className="fs-dlv__muted">{t('Reruns')}: {e.recalculate.join(' → ')}</div>}
                        {e.deliverables && e.deliverables.length > 0 && <div className="fs-dlv__muted">{t('Deliverables')}: {e.deliverables.join(', ')}</div>}
                        {e.effort_hours && <div className="fs-dlv__muted">{e.effort_hours[0]}–{e.effort_hours[1]} h{e.no_effort_for?.length ? ` (${t('no range for')} ${e.no_effort_for.join(', ')})` : ''}</div>}
                      </>
                    ) : '—'}
                  </td>
                  <td>
                    {r.question ? <p className="fs-dlv__ask">{r.question}</p> : <ul className="fs-dlv__plain fs-dlv__small">{r.options.map((o) => <li key={o}>{o}</li>)}</ul>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
      {cmp.questions.length > 0 && (
        <section className="fs-dlv__block" aria-label={t('Questions for the client')}>
          <h3 className="fs-dlv__h">{t('Questions for the client')}</h3>
          <ol className="fs-dlv__plain">{cmp.questions.map((q) => <li key={q}>{q}</li>)}</ol>
        </section>
      )}
      {cmp.negotiated.length > 0 && (
        <section className="fs-dlv__block" aria-label={t('Negotiated from the first request')}>
          <h3 className="fs-dlv__h">{t('Negotiated from the first request')}</h3>
          <ul className="fs-dlv__plain">{cmp.negotiated.map((n) => <li key={n.clause}><StatusPill status={n.status} /> {n.clause}</li>)}</ul>
        </section>
      )}
      <p className="fs-dlv__muted">{cmp.limits}</p>
    </div>
  );
}

export function ScopePanel() {
  const [title, setTitle] = useState('');
  const [initial, setInitial] = useState('');
  const [agreement, setAgreement] = useState('');
  const [request, setRequest] = useState('');
  const [graph, setGraph] = useState('');
  const [mode, setMode] = useState('delta');
  const [effort, setEffort] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [cmp, setCmp] = useState<api.ScopeComparison | null>(null);
  const [history, setHistory] = useState<api.ScopeListRow[]>([]);
  const reload = useCallback(() => { api.listComparisons().then(setHistory).catch(() => undefined); }, []);
  useEffect(() => { reload(); }, [reload]);
  useEffect(() => {
    // A commission links its scope versions here with ?view=scope&c=<comparison id>.
    const id = new URLSearchParams(window.location.search).get('c');
    if (id) api.loadComparison(id).then(setCmp).catch(() => undefined);
  }, []);
  const run = async () => {
    setBusy(true);
    setError('');
    try {
      const eff = api.parseEffort(effort);
      setCmp(await api.compareScope({ title, initial, agreement, new_request: request, graph: graph.trim(), mode,
        ...(Object.keys(eff).length ? { effort_hours: eff } : {}) }));
      reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="fs-dlv__layout" data-testid="scope">
      <div className="fs-dlv__detail">
        <section className="fs-dlv__block" aria-label={t('Compare scope')}>
          <h2 className="fs-dlv__h">{t('Compare scope')}</h2>
          <label className="fs-dlv__field">{t('Title')}<input className="fs-field" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
          <label className="fs-dlv__field">{t('Agreed scope')}<textarea className="fs-field" rows={5} value={agreement} onChange={(e) => setAgreement(e.target.value)} data-testid="scope-agreement" /></label>
          <label className="fs-dlv__field">{t('New request')}<textarea className="fs-field" rows={4} value={request} onChange={(e) => setRequest(e.target.value)} data-testid="scope-request" /></label>
          <label className="fs-dlv__field">{t('First request (optional)')}<textarea className="fs-field" rows={3} value={initial} onChange={(e) => setInitial(e.target.value)} /></label>
          <div className="fs-dlv__row">
            <label className="fs-dlv__field fs-dlv__grow">{t('Decision map (graph id, optional)')}<input className="fs-field" value={graph} onChange={(e) => setGraph(e.target.value)} data-testid="scope-graph" /></label>
            <label className="fs-dlv__field">{t('The new request')}
              <select className="fs-field" value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="delta">{t('adds to the agreement')}</option>
                <option value="replace">{t('restates the whole scope')}</option>
              </select>
            </label>
          </div>
          <label className="fs-dlv__field">{t('Hours per step (optional, "node: min-max" per line)')}<textarea className="fs-field" rows={3} value={effort} onChange={(e) => setEffort(e.target.value)} data-testid="scope-effort" /></label>
          {error && <p className="fs-dlv__why" data-tone="bad" role="alert">{error}</p>}
          <div><Button variant="primary" icon={GitCompareArrows} label={busy ? t('Comparing…') : t('Compare')} onClick={() => { void run(); }}
            disabled={busy || !agreement.trim() || !request.trim()} testId="scope-compare" /></div>
        </section>
        {history.length > 0 && (
          <ul className="fs-dlv__list" aria-label={t('Earlier comparisons')}>
            {history.slice(0, 8).map((h) => (
              <li key={h.id}>
                <button type="button" className="fs-dlv__item" data-active={cmp?.id === h.id} onClick={() => { void api.loadComparison(h.id).then(setCmp); }}>
                  <span className="fs-dlv__item-title">{h.title || h.id}</span>
                  <span className="fs-dlv__muted">{h.created} · {Object.entries(h.counts).filter(([, n]) => n).map(([s, n]) => `${api.scopeLabel(s)} ${n}`).join(' · ')}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        {cmp ? <Result cmp={cmp} /> : <EmptyState icon={GitCompareArrows} title={t('No comparison open')} body={t('Paste the agreed scope and the new request to see what is new, changed or already included.')} />}
      </div>
    </div>
  );
}
