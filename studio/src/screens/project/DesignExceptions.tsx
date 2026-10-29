import { Plus, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { DESIGN_WAIVER_MIN_REASON, getDesignHookStatus, startDesignModule, type DesignHookStatus, type DesignWaiver } from '../../adapters/projects';
import { t } from '../../i18n';

/**
 * The design check's side of the project settings: whether the design
 * workshop answers right now, and the exceptions the project accepts (a rule
 * and why it is fine here). The workshop only refuses an exception without a
 * reason, so the reason is part of the row, not an extra.
 */
export function DesignExceptions({ waivers, onChange }: { waivers: DesignWaiver[]; onChange: (next: DesignWaiver[]) => void }) {
  const [workshop, setWorkshop] = useState<DesignHookStatus | null | 'unknown'>('unknown');
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState('');

  useEffect(() => {
    const ctl = new AbortController();
    getDesignHookStatus(ctl.signal).then(setWorkshop, () => setWorkshop(null));
    return () => ctl.abort();
  }, []);

  const start = async () => {
    setStarting(true);
    setStartError('');
    try {
      const next = await startDesignModule();
      setWorkshop(next);
      if (!next.running) setStartError(t('The design module did not start: {reason}', { reason: next.error ?? next.design_module ?? '?' }));
    } catch (e) {
      setStartError((e as Error).message);
    } finally {
      setStarting(false);
    }
  };

  const ready = workshop !== 'unknown' && workshop !== null && workshop.running;
  const moduleOff = workshop !== 'unknown' && workshop !== null && workshop.workshop_running && !workshop.running;

  const set = (index: number, patch: Partial<DesignWaiver>) => onChange(waivers.map((w, i) => (i === index ? { ...w, ...patch } : w)));

  return (
    <div className="fs-pj__design" data-testid="design-exceptions">
      <small className="fs-pj__design-state" data-state={ready ? 'on' : 'off'} data-testid="design-workshop-state">
        {workshop === 'unknown'
          ? t('Looking for the design workshop…')
          : ready
            ? t('The design workshop answers at {url}.', { url: workshop.workshop_url })
            : moduleOff
              ? t('The design workshop is running but its design module is off: the check stays silent until it is turned on.')
              : t('The design workshop is stopped: start it from the Hub and the check applies from the next edit.')}
      </small>
      {moduleOff && (
        <div className="fs-pj__row">
          <Button variant="secondary" size="sm" label={t('Turn on the design module')} loading={starting} onClick={() => void start()} testId="design-module-start" />
          {startError && <small className="fs-pj__waiver-hint">{startError}</small>}
        </div>
      )}
      <span className="fs-pj__design-title">
        {t('Design exceptions')}
        <small>{t('A rule the project accepts breaking, with the reason. Floor rules cannot be excepted.')}</small>
      </span>
      {waivers.map((w, i) => {
        const short = w.reason.trim().length < DESIGN_WAIVER_MIN_REASON;
        return (
          <div className="fs-pj__waiver" key={i} data-testid="design-waiver">
            <input className="fs-field fs-pj__waiver-rule" value={w.rule} onChange={(e) => set(i, { rule: e.target.value })} placeholder={t('Rule id')} aria-label={t('Rule id')} spellCheck={false} maxLength={80} />
            <input
              className="fs-field fs-pj__grow"
              value={w.reason}
              onChange={(e) => set(i, { reason: e.target.value })}
              placeholder={t('Why it is fine in this project')}
              aria-label={t('Why it is fine in this project')}
              aria-invalid={short}
              maxLength={400}
            />
            <Button variant="ghost" size="sm" icon={X} label={t('Remove exception')} title={t('Remove exception')} onClick={() => onChange(waivers.filter((_, j) => j !== i))} />
            {short && <small className="fs-pj__waiver-hint">{t('The reason needs {n} characters or more.', { n: String(DESIGN_WAIVER_MIN_REASON) })}</small>}
          </div>
        );
      })}
      <div className="fs-pj__row">
        <Button variant="ghost" size="sm" icon={Plus} label={t('Add an exception')} onClick={() => onChange([...waivers, { rule: '', reason: '' }])} testId="design-waiver-add" />
      </div>
    </div>
  );
}
