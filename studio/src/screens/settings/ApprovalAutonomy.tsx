import { Fragment, useEffect, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Button, Skeleton } from '../../components';
import {
  getApprovalAutonomyDecisions,
  getApprovalAutonomyMode,
  getApprovalAutonomyStats,
  setApprovalAutonomyFamily,
  type AutonomyDecision,
  type AutonomyFamilyStat,
  type AutonomyMode,
} from '../../adapters/settings';
import { t } from '../../i18n';

const DECISIONS_LIMIT = 20;

/** The last N shadow-log rows for one family, expanded under its aggregate
 * row: what shadow mode would have decided for each call, and (once a
 * person acted on the card) what actually happened. */
function DecisionsDrilldown({ owner, family }: { owner: string; family: string }) {
  const [rows, setRows] = useState<AutonomyDecision[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getApprovalAutonomyDecisions(owner, family, DECISIONS_LIMIT)
      .then((d) => { if (!cancelled) setRows(d); })
      .catch((e: Error) => { if (!cancelled) setErr(e.message); });
    return () => { cancelled = true; };
  }, [owner, family]);

  if (err) return <p className="fs-set__err">{err}</p>;
  if (!rows) return <Skeleton label={t('Loading')} count={2} height="24px" />;
  if (rows.length === 0) return <p className="fs-set__help">{t('No decisions logged for this family yet.')}</p>;

  return (
    <table className="fs-autonomy-table fs-autonomy-table--nested">
      <thead>
        <tr>
          <th>{t('Time')}</th>
          <th>{t('Tool')}</th>
          <th>{t('Shadow would have')}</th>
          <th>{t('Actually happened')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>{new Date(r.created_at).toLocaleString()}</td>
            <td><code>{r.tool_name}</code></td>
            <td>
              {r.tier === 'act' ? t('auto-approve') : r.tier === 'advise' ? t('lean approve, still ask') : t('escalate')}
              {' '}({Math.round(r.score * 100)}%)
            </td>
            <td>
              {r.actual_decision === null
                ? t('pending')
                : r.actual_decision === 'approved' ? t('approved') : t('denied')}
              {r.agreed !== null && (
                <span className="fs-set__help" data-tone={r.agreed ? 'ok' : 'bad'}>
                  {' '}{r.agreed ? t('agreed') : t('disagreed')}
                </span>
              )}
              {r.destructive && <span className="fs-set__help" data-tone="bad"> · {t('destructive')}</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Feature 2 (shadow mode / confidence-tiered approvals): the mode toggle
 * (`approval_autonomy`) already renders itself in the "Agent" schema list
 * above this card, since it is a normal `agent_settings_schema.py` group.
 * What that generic list cannot show is the per-family shadow-log history —
 * how often each tool family's logged decision agreed with what the person
 * actually did, and whether it has earned (or been manually given) auto-
 * approval — so this card reads `/api/approval-autonomy/{mode,stats}` and
 * lets an admin promote/demote a family by hand (`POST .../family`, gated
 * `require_human` server-side: the agent's own loopback token cannot call
 * it, mirroring the approval-card grant/deny split).
 */
export function ApprovalAutonomySection({ say }: { say: (t: string) => void }) {
  const [mode, setMode] = useState<AutonomyMode | null>(null);
  const [rows, setRows] = useState<AutonomyFamilyStat[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [openRow, setOpenRow] = useState<string | null>(null);

  const load = () => {
    setErr(null);
    Promise.all([getApprovalAutonomyMode(), getApprovalAutonomyStats()])
      .then(([m, families]) => {
        setMode(m);
        setRows(families);
      })
      .catch((e: Error) => setErr(e.message));
  };
  useEffect(load, []);

  const toggle = (owner: string, family: string, promoted: boolean) => {
    const key = `${owner}/${family}`;
    setBusy(key);
    setApprovalAutonomyFamily(owner, family, promoted ? '' : 'promoted')
      .then(() => { say(promoted ? t('Demoted back to computed value.') : t('Promoted — this family may now be auto-approved.')); load(); })
      .catch((e: Error) => say(e.message))
      .finally(() => setBusy(null));
  };

  return (
    <div className="fs-set__card">
      <h3 className="fs-set__card-title">{t('Approval autonomy — shadow log')}</h3>
      <p className="fs-set__help">
        {t('Per tool family: how many gated calls this logged, how often the confidence score agreed with what was actually decided, and whether the family is promoted for auto-approval in "active" mode. A family with any disagreement on a destructive call is never auto-promoted.')}
      </p>
      {mode && (
        <p className="fs-set__help">
          {t('Current mode: {mode}. Auto-approve threshold {act}, promotion needs {n}+ confident decisions at {pct}% agreement.', {
            mode: mode.mode,
            act: mode.act_threshold,
            n: mode.promote_min_decisions,
            pct: Math.round(mode.promote_min_agreement * 100),
          })}
        </p>
      )}
      {err && <p className="fs-set__err">{err}</p>}
      {!rows && !err ? (
        <p className="fs-set__help">{t('Loading')}</p>
      ) : rows && rows.length === 0 ? (
        <p className="fs-set__help">{t('No shadow-log history yet — it fills in as gated calls are decided, in any mode.')}</p>
      ) : (
        <table className="fs-autonomy-table">
          <thead>
            <tr>
              <th>{t('Owner')}</th>
              <th>{t('Family')}</th>
              <th>{t('Decisions')}</th>
              <th>{t('Confident (act-tier)')}</th>
              <th>{t('Agreement')}</th>
              <th>{t('Status')}</th>
              <th />
              <th />
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((r) => {
              const key = `${r.owner}/${r.family}`;
              const open = openRow === key;
              return (
                <Fragment key={key}>
                  <tr>
                    <td>{r.owner || t('(no owner)')}</td>
                    <td><code>{r.family}</code></td>
                    <td>{r.total}</td>
                    <td>{r.act_total}</td>
                    <td>{r.act_total ? `${Math.round(r.agreement_rate * 100)}%` : '—'}</td>
                    <td>
                      <span className="fs-autonomy-badge" data-tone={r.promoted ? 'promoted' : 'not-promoted'}>
                        {r.promoted ? t('promoted') : t('not promoted')}
                      </span>
                      {r.override && <span className="fs-set__help"> ({t('manual')})</span>}
                    </td>
                    <td>
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={busy === key}
                        disabled={busy !== null}
                        label={r.promoted ? t('Demote') : t('Promote')}
                        onClick={() => toggle(r.owner, r.family, r.promoted)}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="fs-autonomy-expand"
                        aria-expanded={open}
                        aria-label={t('Show recent decisions')}
                        onClick={() => setOpenRow(open ? null : key)}
                        data-testid={`autonomy-drilldown-toggle-${r.family}`}
                      >
                        <ChevronDown size={14} aria-hidden="true" style={{ transform: open ? 'rotate(180deg)' : undefined }} />
                      </button>
                    </td>
                  </tr>
                  {open && (
                    <tr>
                      <td colSpan={8} className="fs-autonomy-drilldown">
                        <DecisionsDrilldown owner={r.owner} family={r.family} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
