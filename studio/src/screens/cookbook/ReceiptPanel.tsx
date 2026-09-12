import { Check, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { getServeReceipt, patchTask, receiptTone, summarizeReceipt, verifyServeReceipt, baseUrlFromCmd, type CapabilityAssessment, type LaunchReceipt } from '../../adapters/cookbook';
import { t } from '../../i18n';
import type { Task } from '../../lib/cookbook/tasks';

/**
 * INF-02 §07/§15: "requested vs applied", for one serve task. Reads the
 * receipt `POST /api/model/serve` filed (`GET .../receipt`) and lets an
 * admin re-probe it (`POST .../verify`) — both read-only calls; nothing
 * here starts, stops or reconfigures the process.
 *
 * The first time this task is observed `ready`, it fires one automatic
 * (unauthorized) verify — guarded by `task._receiptAutoVerified`, a flag
 * persisted on the task itself so a remount, a reconnect, or a later
 * re-render never re-fires it (T19). `Run authorized probe` is never
 * automatic: it sends a real 1-token request, and only after the admin
 * confirms that explicitly, every single time.
 */
export function ReceiptPanel({ task, say }: { task: Task; say: (m: string) => void }) {
  const [receipt, setReceipt] = useState<LaunchReceipt | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [confirmProbe, setConfirmProbe] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    getServeReceipt(task.sessionId)
      .then((r) => {
        if (!cancelled) setReceipt(r);
      })
      .catch(() => {
        if (!cancelled) setReceipt(null);
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [task.sessionId]);

  const runVerify = async (authorizedProbe: boolean) => {
    setVerifying(true);
    try {
      const cmd = task.payload?.final_cmd || task.payload?._cmd || '';
      const r = await verifyServeReceipt(task.sessionId, { baseUrl: baseUrlFromCmd(cmd), authorizedProbe });
      setReceipt(r);
    } catch (e) {
      say((e as Error).message);
    } finally {
      setVerifying(false);
      setConfirmProbe(false);
    }
  };

  // T19: exactly once per session, the first time it is seen `ready` — never
  // on every render/reconnect. The flag lives on the task itself (server-
  // persisted cookbook state), not component state, so it survives a remount.
  useEffect(() => {
    if (task.status !== 'ready' || task._receiptAutoVerified) return;
    patchTask(task.sessionId, { _receiptAutoVerified: true });
    void runVerify(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.status, task.sessionId, task._receiptAutoVerified]);

  if (!loaded || !receipt) return null;

  const tone = receiptTone(receipt);

  return (
    <div className="fs-ck__receipt" data-testid="serve-receipt" data-tone={tone}>
      <div className="fs-ck__receipt-head">
        <span className="fs-ck__label">{t('Requested vs applied')}</span>
        <span className="fs-ck__receipt-verify-state" data-tone={tone}>
          {t(receipt.verify_state)}
        </span>
      </div>
      <p className="fs-muted">
        {t('Implementation: {impl}', { impl: receipt.engine.implementation })}
        {receipt.engine.version ? ` · ${receipt.engine.version}` : ''}
        {receipt.engine.build ? ` · ${receipt.engine.build}` : ''}
      </p>

      {receipt.assessments.length > 0 ? (
        <div className="fs-ck__table-wrap">
          <table className="fs-ck__table">
            <thead>
              <tr>
                <th>{t('Option')}</th>
                <th>{t('Requested')}</th>
                <th>{t('Observed')}</th>
                <th>{t('State')}</th>
              </tr>
            </thead>
            <tbody>
              {receipt.assessments.map((a) => (
                <tr key={a.option}>
                  <td>
                    <code>{a.option}</code>
                  </td>
                  <td>{String(a.requested ?? '')}</td>
                  <td>{a.effective.value === null || a.effective.value === undefined ? '—' : String(a.effective.value)}</td>
                  <td>
                    <EffectiveState a={a} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="fs-muted">{t('No structured plan was recorded for this launch.')}</p>
      )}

      {receipt.checks.length > 0 && (
        <ul className="fs-ck__receipt-checks">
          {receipt.checks.map((c) => (
            <li key={c.name} data-state={c.state}>
              <code>{c.name}</code> — {t(c.state)}
              {c.detail ? ` (${c.detail})` : ''}
            </li>
          ))}
        </ul>
      )}

      <p className="fs-muted">{summarizeReceipt(receipt)}</p>

      <div className="fs-inline">
        <Button variant="secondary" size="sm" label={t('Verify now')} loading={verifying} onClick={() => void runVerify(false)} testId="serve-verify" />
        {!confirmProbe ? (
          <Button variant="ghost" size="sm" label={t('Run authorized probe (one token)')} onClick={() => setConfirmProbe(true)} testId="serve-verify-probe" />
        ) : (
          <>
            <span className="fs-muted">{t('Sends a single real request (max_tokens: 1) to the running model. Continue?')}</span>
            <Button variant="danger" size="sm" label={t('Confirm probe')} loading={verifying} onClick={() => void runVerify(true)} testId="serve-verify-probe-confirm" />
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirmProbe(false)} />
          </>
        )}
      </div>
    </div>
  );
}

/** "confirmed ✓, mismatch ✗ with both values, unconfirmed «engine does not
 *  expose this»" — the state cell's exact content, per option. */
function EffectiveState({ a }: { a: CapabilityAssessment }) {
  if (a.effective.state === 'confirmed') {
    return (
      <span className="fs-ck__eff" data-state="confirmed">
        <Check size={13} aria-hidden="true" /> {t('Confirmed')}
      </span>
    );
  }
  if (a.effective.state === 'mismatch') {
    return (
      <span className="fs-ck__eff" data-state="mismatch">
        <X size={13} aria-hidden="true" /> {t('Mismatch: requested {requested}, observed {observed}', { requested: String(a.requested ?? ''), observed: String(a.effective.value ?? '') })}
      </span>
    );
  }
  if (a.effective.state === 'not_applicable') {
    return (
      <span className="fs-ck__eff" data-state="not_applicable">
        {t('Not applicable')}
      </span>
    );
  }
  return (
    <span className="fs-ck__eff" data-state="unconfirmed">
      {t('engine does not expose this')}
    </span>
  );
}
