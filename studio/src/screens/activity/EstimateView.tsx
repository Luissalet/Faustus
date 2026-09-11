import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components/Button';
import {
  comparePlans, workflowEstimateDetailed,
  type DetailedEstimate, type DetailedEstimatePerNode, type PlanBasis, type PlanComparison,
  type PlanInput, type WorkflowEstimate,
} from '../../adapters/topology';
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
 *
 * W3-C (CMP-08 follow-up, `CONTRATO_W3.md`): the two new props below are
 * OPTIONAL so `Activity.tsx` — a file this lot does not own and must not
 * touch — keeps compiling and rendering exactly what it renders today with
 * zero edits. When a caller (today: nobody; this is a wiring point left for
 * the orchestrator — see the informe) passes `definition`, this view
 * fetches `estimate_detailed()`'s separated accounts, lists every
 * assumption and every reason a total is incomplete (`unknown` is shown as
 * "desconocido", never as 0 or "gratis"), shows "previsto vs real" once a
 * `runId` has a measured run, and offers "Comparar planes" against a
 * single-model and a deterministic-steps variant of the same graph.
 */
export function usd(value: number): string {
  return value.toLocaleString(locale(), { style: 'currency', currency: 'USD', maximumFractionDigits: 4 });
}

function fmtNum(value: number): string {
  return value.toLocaleString(locale());
}

function fmtRange(min: number, max: number): string {
  return min === max ? fmtNum(min) : `${fmtNum(min)} – ${fmtNum(max)}`;
}

/** `"unknown"` (a real signal absent) is never rendered as a blank or a
 * zero — it says so, in the current language. */
function fmtMaybeUnknown(value: unknown): string {
  if (value === 'unknown' || value === null || value === undefined || value === '') return t('unknown');
  if (typeof value === 'number') return fmtNum(value);
  if (typeof value === 'object') {
    const row = value as Record<string, unknown>;
    if ('available' in row || 'foreground_waiting' in row) {
      return t('{available} free / {waiting} waiting', {
        available: fmtMaybeUnknown(row.available), waiting: fmtMaybeUnknown(row.foreground_waiting),
      });
    }
    return JSON.stringify(value);
  }
  return String(value);
}

interface WorkflowEstimateViewProps {
  estimate: WorkflowEstimate;
  /** The workflow definition this `estimate` was computed for. */
  definition?: Record<string, unknown>;
  /** The run this estimate opened from, if any — enables "previsto vs
   * real" once that run has recorded usage. */
  runId?: string;
}

export function WorkflowEstimateView({ estimate, definition, runId }: WorkflowEstimateViewProps) {
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
      {definition && <DetailedEstimateSection definition={definition} runId={runId} />}
    </div>
  );
}

function AccountRow({ label, value, format }: {
  label: string; value: { min: number; max: number }; format?: (n: number) => string;
}) {
  const fmt = format ?? fmtNum;
  return (
    <div className="fs-estimate__account">
      <span className="fs-estimate__account-label">{label}</span>
      <span className="fs-estimate__account-value">
        {value.min === value.max ? fmt(value.min) : `${fmt(value.min)} – ${fmt(value.max)}`}
      </span>
    </div>
  );
}

function PerNodeRow({ node }: { node: DetailedEstimatePerNode }) {
  const unknown = node.callsProfileSource === 'unknown';
  return (
    <tr>
      <td><code>{node.nodeId}</code></td>
      <td>{node.type}</td>
      <td>{node.model || '—'}</td>
      <td>{fmtRange(node.activations.min, node.activations.max)}</td>
      <td>{unknown ? fmtMaybeUnknown('unknown') : fmtRange(node.modelCalls.min, node.modelCalls.max)}</td>
      <td>{fmtRange(node.externalOps.min, node.externalOps.max)}</td>
      <td>
        {unknown ? fmtMaybeUnknown('unknown')
          : `${fmtRange(node.tokensIn.min, node.tokensIn.max)} / ${fmtRange(node.tokensOut.min, node.tokensOut.max)}`}
      </td>
      <td>
        {node.latencyEstimate ? (
          <span className="fs-estimate__latency">
            {t('load')} {fmtMaybeUnknown(node.latencyEstimate.load)} ·{' '}
            {t('gen')} {typeof node.latencyEstimate.generation_tps === 'number'
              ? t('{n} tok/s', { n: fmtNum(node.latencyEstimate.generation_tps as number) })
              : fmtMaybeUnknown(node.latencyEstimate.generation_tps)} ·{' '}
            {t('queue')} {fmtMaybeUnknown(node.latencyEstimate.queue)}
          </span>
        ) : '—'}
      </td>
      <td>
        {node.usd.min === node.usd.max ? usd(node.usd.min) : `${usd(node.usd.min)} – ${usd(node.usd.max)}`}
        {node.note && <span className="fs-estimate__note"> — {node.note}</span>}
      </td>
    </tr>
  );
}

const COMPARISON_METRIC_LABELS: Record<string, string> = {
  node_activations_max: 'Node activations (max)',
  model_calls_max: 'Model calls (max)',
  external_ops_max: 'External operations (max)',
  tokens_in_max: 'Tokens in (max)',
  tokens_out_max: 'Tokens out (max)',
  cost_known_usd_max: 'Known cost, USD (max)',
};

function basisLabel(basis: PlanBasis): string {
  if (basis === 'computed') return t('computed');
  if (basis === 'estimated') return t('estimated');
  return t('unknown');
}

function PlanComparisonTable({ comparison }: { comparison: PlanComparison }) {
  return (
    <div className="fs-estimate__table-wrap">
      <table className="fs-estimate__table" data-testid="estimate-plan-comparison">
        <thead>
          <tr>
            <th>{t('Metric')}</th>
            {comparison.planIds.map((id) => <th key={id}>{comparison.planLabels[id] ?? id}</th>)}
          </tr>
        </thead>
        <tbody>
          {comparison.rows.map((row) => (
            <tr key={row.metric}>
              <td>{t(COMPARISON_METRIC_LABELS[row.metric] ?? row.metric)}</td>
              {comparison.planIds.map((id) => {
                const cell = row.cells[id];
                if (!cell) return <td key={id}>—</td>;
                const shown = row.metric === 'cost_known_usd_max' && typeof cell.value === 'number'
                  ? usd(cell.value) : String(cell.value);
                return (
                  <td key={id}>
                    <span className="fs-estimate__basis" data-basis={cell.basis}>{shown}</span>
                    <span className="fs-estimate__basis-label"> ({basisLabel(cell.basis)})</span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {Object.keys(comparison.errors).length > 0 && (
        <div className="fs-notice" data-tone="warning" role="status">
          {Object.entries(comparison.errors).map(([id, err]) => (
            <p key={id}>{comparison.planLabels[id] ?? id}: {err}</p>
          ))}
        </div>
      )}
    </div>
  );
}

type WorkflowNodeLike = { type?: string; config?: Record<string, unknown> } & Record<string, unknown>;

function cloneDefinition(definition: Record<string, unknown>): Record<string, unknown> {
  return JSON.parse(JSON.stringify(definition));
}

function skillNodes(definition: Record<string, unknown>): WorkflowNodeLike[] {
  const nodes = Array.isArray(definition.nodes) ? (definition.nodes as WorkflowNodeLike[]) : [];
  return nodes.filter((n) => n && n.type === 'skill');
}

/**
 * W3-C: two heuristic variants of the CURRENT definition for the "Comparar
 * planes" button. `src/plan_compare.py::compare` accepts any definition it
 * is handed and does not generate "single model"/"deterministic steps"
 * alternatives itself (see that module's docstring) — building them is the
 * caller's job, and this is the honest, documented way this view does it:
 * both variants keep the exact same nodes and `needs` (the same graph
 * shape as the current plan), only what each `skill` node's own `config`
 * says changes, so the comparison is about "what would these steps cost
 * differently" rather than a silently different plan shape.
 *
 * `single_model`: every `skill` node's `config.model` is overridden to
 * whichever model the current plan already names most often (a no-op when
 * every skill node already agrees, or when none name a model at all).
 * `deterministic_steps`: every `skill` node becomes a `manual` node with an
 * empty `config` — "as much as possible pushed into non-model node types"
 * (`plan_compare.py`'s own phrase), at the cost of no longer being a
 * runnable plan; that is the point of the comparison, not a bug in it.
 */
function buildComparisonPlans(definition: Record<string, unknown>): PlanInput[] {
  const current: PlanInput = { id: 'current', label: t('Current plan'), definition };

  const modelCounts = new Map<string, number>();
  for (const node of skillNodes(definition)) {
    const model = String(node.config?.model || '');
    if (model) modelCounts.set(model, (modelCounts.get(model) ?? 0) + 1);
  }
  let singleModel = '';
  let bestCount = -1;
  for (const [model, count] of modelCounts) {
    if (count > bestCount) { singleModel = model; bestCount = count; }
  }

  const singleModelDef = cloneDefinition(definition);
  if (singleModel) {
    for (const node of skillNodes(singleModelDef)) {
      const config = (node.config && typeof node.config === 'object') ? { ...node.config } : {};
      config.model = singleModel;
      node.config = config;
    }
  }

  const deterministicDef = cloneDefinition(definition);
  for (const node of skillNodes(deterministicDef)) {
    node.type = 'manual';
    node.config = {};
  }

  return [
    current,
    { id: 'single_model', label: t('Single model'), definition: singleModelDef },
    { id: 'deterministic_steps', label: t('Deterministic steps'), definition: deterministicDef },
  ];
}

function DetailedEstimateSection({ definition, runId }: { definition: Record<string, unknown>; runId?: string }) {
  const [detail, setDetail] = useState<DetailedEstimate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [comparison, setComparison] = useState<PlanComparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    setComparison(null);
    setCompareError(null);
    setBusy(true);
    workflowEstimateDetailed(definition, runId ? { runId } : undefined)
      .then((result) => { if (!cancelled) setDetail(result); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [definition, runId]);

  const runCompare = useCallback(() => {
    setComparing(true);
    setCompareError(null);
    comparePlans(t('Cost of the current workflow'), buildComparisonPlans(definition))
      .then(setComparison)
      .catch((e) => setCompareError(e instanceof Error ? e.message : String(e)))
      .finally(() => setComparing(false));
  }, [definition]);

  if (busy) return <p className="fs-muted">{t('Loading the detailed estimate…')}</p>;
  if (error) return <div className="fs-notice" data-tone="warning" role="status">{error}</div>;
  if (!detail) return null;

  return (
    <div className="fs-estimate__detail">
      <h4 className="fs-estimate__section-title">{t('Separate accounts')}</h4>
      <p className="fs-act__hint">
        {t('Node activations, model calls and external operations are different things — a "deliver" or "artifact_store" node is an external operation, never a model call.')}
      </p>
      <div className="fs-estimate__accounts">
        <AccountRow label={t('Node activations')} value={detail.nodeActivations} />
        <AccountRow label={t('Model calls')} value={detail.modelCalls} />
        <AccountRow label={t('External operations')} value={detail.externalOps} />
        <AccountRow label={t('Tokens in')} value={detail.tokens.in} />
        <AccountRow label={t('Tokens out')} value={detail.tokens.out} />
        <AccountRow label={t('Known cost (USD)')} value={detail.costKnownUsd} format={usd} />
      </div>

      {detail.assumptions.length > 0 && (
        <div className="fs-notice" data-tone="warning" role="status">
          <strong>{t('Assumptions used')}:</strong>
          <ul className="fs-estimate__list">
            {detail.assumptions.map((a, i) => <li key={i}>{a}</li>)}
          </ul>
        </div>
      )}

      {detail.costUnestimable.length > 0 && (
        <div className="fs-notice" role="status">
          <strong>{t('Unknown — never shown as zero or free')}:</strong>
          <ul className="fs-estimate__list">
            {detail.costUnestimable.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}

      {detail.measured && (
        <div className="fs-estimate__measured">
          <h4 className="fs-estimate__section-title">{t('Forecast vs actual')}</h4>
          <p>
            {t('Forecast (max): {n} node activation(s)', { n: String(detail.nodeActivations.max) })}
            {' · '}
            {detail.measured.toolCalls === null
              ? t('actual tool calls: {v}', { v: t('unknown') })
              : t('actual: {n} tool call(s)', { n: String(detail.measured.toolCalls) })}
          </p>
          {detail.measured.activeSeconds !== null && (
            <p>{t('Actual active time: {s}s', { s: String(detail.measured.activeSeconds) })}</p>
          )}
          <p className="fs-act__hint">{detail.measured.note}</p>
        </div>
      )}

      <details className="fs-estimate__per-node">
        <summary>{t('Per-node breakdown')}</summary>
        <div className="fs-estimate__table-wrap">
          <table className="fs-estimate__table">
            <thead>
              <tr>
                <th>{t('Node')}</th>
                <th>{t('Type')}</th>
                <th>{t('Model')}</th>
                <th>{t('Activations')}</th>
                <th>{t('Model calls')}</th>
                <th>{t('External ops')}</th>
                <th>{t('Tokens in/out')}</th>
                <th>{t('Local latency')}</th>
                <th>{t('Cost (USD)')}</th>
              </tr>
            </thead>
            <tbody>
              {detail.perNode.map((n) => <PerNodeRow key={n.nodeId} node={n} />)}
            </tbody>
          </table>
        </div>
      </details>

      <div className="fs-estimate__compare">
        <Button
          variant="secondary" size="sm" label={t('Compare plans')} onClick={runCompare}
          loading={comparing} testId="estimate-compare-plans"
        />
        {compareError && <div className="fs-notice" data-tone="warning" role="status">{compareError}</div>}
        {comparison && <PlanComparisonTable comparison={comparison} />}
      </div>
    </div>
  );
}
