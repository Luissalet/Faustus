import { type WorkflowEstimate } from '../../adapters/topology';
import { locale, t, tn } from '../../i18n';

/**
 * B2 (OBJ-8): `POST /api/workflows/estimate`'s `Estimate`
 * (`docs/api/topology.md`'s "Cost estimate" section) — a min/max range, not
 * one number, because a `condition` branch or a cycle genuinely might or
 * might not run. `unpricedModels` and each `skill` node's own blank `note`
 * are shown rather than folded away: a model missing from the caller's
 * price map contributes $0 to the total, and that is a fact about the
 * estimate the person needs to see, not a rounding error to hide.
 *
 * Moved out of `Activity.tsx` (W2-B, `CONTRATO_CMP_W2.md`) so CMP-08's
 * `Activity.tsx`-owning neighbour (W2-C) never has to touch this component
 * while working on the rest of that screen — `Activity.tsx` now only
 * imports it. Behaviour unchanged: same props, same markup, same test id
 * (`workflow-estimate`).
 */
export function usd(value: number): string {
  return value.toLocaleString(locale(), { style: 'currency', currency: 'USD', maximumFractionDigits: 4 });
}

export function WorkflowEstimateView({ estimate }: { estimate: WorkflowEstimate }) {
  return (
    <div className="fs-estimate" data-testid="workflow-estimate">
      <p className="fs-estimate__total">
        {t('Estimated total: {range} · {calls} calls', {
          range: estimate.totalUsdMin === estimate.totalUsdMax ? usd(estimate.totalUsdMin) : `${usd(estimate.totalUsdMin)} – ${usd(estimate.totalUsdMax)}`,
          calls: estimate.callsMin === estimate.callsMax ? String(estimate.callsMin) : `${estimate.callsMin}–${estimate.callsMax}`,
        })}
      </p>
      <p className="fs-act__hint">{t('Only skill nodes are priced; every other node type is structural and costs nothing by itself.')}</p>
      <div className="fs-estimate__table-wrap">
        <table className="fs-estimate__table">
          <thead>
            <tr>
              <th>{t('Node')}</th>
              <th>{t('Type')}</th>
              <th>{t('Model')}</th>
              <th>{t('Calls')}</th>
              <th>{t('Cost (USD)')}</th>
            </tr>
          </thead>
          <tbody>
            {estimate.perNode.map((n) => (
              <tr key={n.nodeId}>
                <td><code>{n.nodeId}</code></td>
                <td>{n.type}</td>
                <td>{n.model || '—'}</td>
                <td>{n.callsMin === n.callsMax ? n.callsMin : `${n.callsMin}–${n.callsMax}`}</td>
                <td>
                  {n.usdMin === n.usdMax ? usd(n.usdMin) : `${usd(n.usdMin)} – ${usd(n.usdMax)}`}
                  {n.note && <span className="fs-estimate__note"> — {n.note}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {estimate.unboundedLoops.length > 0 && (
        <div className="fs-notice" data-tone="warning" role="status">
          <strong>{t('Unbounded loops')}:</strong>{' '}
          {estimate.unboundedLoops.map((loop, i) => (
            <span key={i}>
              {i > 0 && '; '}
              {loop.nodes.join(' → ')} ({tn(loop.assumedIterations, 'assumed {n} iteration for the maximum', 'assumed {n} iterations for the maximum')})
            </span>
          ))}
        </div>
      )}
      {estimate.unpricedModels.length > 0 && (
        <p className="fs-act__hint">{t('Models without a price (contribute $0 to this estimate)')}: {estimate.unpricedModels.join(', ')}</p>
      )}
    </div>
  );
}
