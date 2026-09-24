import { useEffect, useState } from 'react';
import { Button } from '../../components';
import {
  getApprovalAutonomyMode,
  getApprovalAutonomyStats,
  setApprovalAutonomyFamily,
  type AutonomyFamilyStat,
  type AutonomyMode,
} from '../../adapters/settings';
import { t } from '../../i18n';

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
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((r) => (
              <tr key={`${r.owner}/${r.family}`}>
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
                    loading={busy === `${r.owner}/${r.family}`}
                    disabled={busy !== null}
                    label={r.promoted ? t('Demote') : t('Promote')}
                    onClick={() => toggle(r.owner, r.family, r.promoted)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
