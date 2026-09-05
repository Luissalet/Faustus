import { useState } from 'react';
import { Button, Dialog } from '../../components';
import { scheduleServe } from '../../adapters/cookbook';
import { t } from '../../i18n';
import { Field, Switch } from './parts';

/**
 * Serve on a timetable: a scheduled task (`cookbook_serve`) that launches
 * the model at a time on chosen days and stops it when the window ends,
 * mirrored to a "Cookbook" calendar when asked.
 */
const DAYS: [string, string][] = [
  ['MO', 'Mon'],
  ['TU', 'Tue'],
  ['WE', 'Wed'],
  ['TH', 'Thu'],
  ['FR', 'Fri'],
  ['SA', 'Sat'],
  ['SU', 'Sun'],
];

export function ScheduleDialog({ repo, host, onClose, say }: { repo: string; host: string; onClose: () => void; say: (m: string) => void }) {
  const [start, setStart] = useState('09:00');
  const [end, setEnd] = useState('18:00');
  const [days, setDays] = useState<string[]>(['MO', 'TU', 'WE', 'TH', 'FR']);
  const [mirror, setMirror] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const title = repo.includes('/') ? repo.split('/').pop()! : repo;

  const save = async () => {
    if (!/^\d\d:\d\d$/.test(start) || !/^\d\d:\d\d$/.test(end)) return setError(t('Start and end must be HH:MM'));
    if (!days.length) return setError(t('Pick at least one day'));
    setBusy(true);
    setError(null);
    try {
      await scheduleServe({ title, repoId: repo, host, startTime: start, endTime: end, days, mirrorToCalendar: mirror });
      say(t('Scheduled: Serve {name} — see Automations', { name: title }));
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => {
        if (!o) onClose();
      }}
      title={t('Serve {name} on a schedule', { name: title })}
      description={t('It launches at the start time on those days and stops when the window ends. The launch settings saved for this model are used.')}
      testId="schedule-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onClose} />
          <Button variant="primary" size="sm" label={t('Save schedule')} loading={busy} onClick={() => void save()} testId="schedule-save" />
        </>
      }
    >
      <div className="fs-ck__grid">
        <Field label={t('Start')}>
          <input className="fs-field" type="time" value={start} onChange={(e) => setStart(e.target.value)} />
        </Field>
        <Field label={t('End')}>
          <input className="fs-field" type="time" value={end} onChange={(e) => setEnd(e.target.value)} />
        </Field>
        <div className="fs-ck__field" data-wide>
          <span className="fs-ck__label">{t('Days')}</span>
          <div className="fs-gal__chips" role="group" aria-label={t('Days')}>
            {DAYS.map(([k, l]) => (
              <button key={k} type="button" className="fs-chip" data-on={days.includes(k) || undefined} onClick={() => setDays((d) => (d.includes(k) ? d.filter((x) => x !== k) : [...d, k]))}>
                {t(l)}
              </button>
            ))}
          </div>
        </div>
        <Switch label={t('Also put it on the Cookbook calendar')} checked={mirror} onChange={setMirror} />
      </div>
      {error && (
        <p className="fs-notice" data-tone="danger" role="alert">
          {error}
        </p>
      )}
    </Dialog>
  );
}
