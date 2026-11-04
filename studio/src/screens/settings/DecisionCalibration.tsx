import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components';
import {
  calibrateDecision, forgetCalibration, loadCalibrations, parseSamples,
  type CalibrationResult, type StoredCalibration,
} from '../../adapters/decisions';
import { t } from '../../i18n';

/** Picks the confidence threshold of a typed (closed-question) decision from labelled examples, with a
 *  Clopper-Pearson bound on the error rate of the answers it accepts. */
export function DecisionCalibration() {
  const [field, setField] = useState('');
  const [text, setText] = useState('');
  const [maxError, setMaxError] = useState('0.05');
  const [delta, setDelta] = useState('0.05');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CalibrationResult | null>(null);
  const [error, setError] = useState('');
  const [stored, setStored] = useState<StoredCalibration[]>([]);

  const refresh = useCallback(async () => {
    try { setStored(await loadCalibrations()); } catch { setStored([]); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  const run = async (apply: boolean) => {
    setBusy(true); setError('');
    try {
      const samples = parseSamples(text);
      if (!samples.length) { setError(t('Write one example per line: the confidence, then 1 if the answer was right or 0 if it was wrong.')); return; }
      if (apply && !field.trim()) { setError(t('Name the question to store its threshold.')); return; }
      setResult(await calibrateDecision({ field: field.trim(), samples, maxError: Number(maxError) || 0.05, delta: Number(delta) || 0.05, apply }));
      if (apply) await refresh();
    } catch {
      setError(t('Could not calibrate.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="fs-set__group" data-testid="decision-calibration">
      <summary className="fs-set__group-head">{t('Calibrate a typed decision')}</summary>
      <p className="fs-set__help">{t('Closed-question decisions accept an answer above a confidence threshold. Give labelled examples and get the lowest threshold whose accepted answers are wrong less often than the rate you choose, with a statistical bound.')}</p>
      <div className="fs-set__group-body">
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="dc-field">{t('Question name')}</label>
          <input id="dc-field" value={field} onChange={(e) => setField(e.target.value)} />
        </div>
        <div className="fs-set__field">
          <label className="fs-set__label" htmlFor="dc-samples">{t('Examples (confidence, 1 right or 0 wrong)')}</label>
          <textarea id="dc-samples" rows={6} value={text} onChange={(e) => setText(e.target.value)} placeholder={'0.97, 1\n0.62, 0'} />
        </div>
        <div className="fs-set__inline">
          <label className="fs-set__label" htmlFor="dc-err">{t('Highest error rate')}</label>
          <input id="dc-err" size={5} value={maxError} onChange={(e) => setMaxError(e.target.value)} />
          <label className="fs-set__label" htmlFor="dc-delta">{t('Risk of the bound being wrong')}</label>
          <input id="dc-delta" size={5} value={delta} onChange={(e) => setDelta(e.target.value)} />
        </div>
        <div className="fs-set__inline">
          <Button variant="secondary" size="sm" label={t('Calibrate')} loading={busy} onClick={() => void run(false)} />
          <Button variant="primary" size="sm" label={t('Calibrate and store')} loading={busy} onClick={() => void run(true)} />
        </div>
        {error && <p className="fs-set__help" role="alert">{error}</p>}
        {result && (
          <p className="fs-set__help" data-testid="decision-calibration-result">
            {result.ok
              ? t('Threshold {threshold}: {accepted} of {samples} examples accepted ({coverage}%), {errors} wrong, error rate below {bound} with {conf}% confidence.', {
                threshold: result.threshold ?? '', accepted: result.accepted, samples: result.samples,
                coverage: Math.round(result.coverage * 100), errors: result.errors,
                bound: result.upperBound ?? '', conf: Math.round((1 - result.delta) * 100),
              })
              : `${t('No threshold gives that guarantee.')} ${result.reason}`}
            {result.saved?.ok ? ` ${t('Stored.')}` : ''}
          </p>
        )}
        {stored.length > 0 && (
          <ul className="fs-set__help" data-testid="decision-calibration-stored">
            {stored.map((c) => (
              <li key={c.field}>
                <code>{c.field}</code> {c.threshold} · {t('{n} examples', { n: c.samples })}
                {' '}<Button variant="ghost" size="sm" label={t('Remove')} onClick={() => { void forgetCalibration(c.field).then(refresh); }} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </details>
  );
}
