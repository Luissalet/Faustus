import { ChevronDown, ChevronRight, Moon, Square } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import {
  getNightShiftReport,
  listNightShifts,
  startNightShift,
  stopNightShift,
  type NightShift,
} from '../../adapters/nightShift';
import { t } from '../../i18n';

/**
 * Night shift (lot E): queue up a short list of tasks to run unattended,
 * sequentially, under a budget, with a Markdown morning report
 * (`docs/api/night_shift.md`, `src/night_shift.py`). A compact sibling of
 * the dispatch form above it in `Workers.tsx` -- same folder, its own queue.
 */

const STATE_WORD: Record<NightShift['state'], string> = {
  queued: 'queued',
  running: 'running',
  done: 'done',
  stopped: 'stopped',
  budget_exhausted: 'budget exhausted',
  error: 'error',
};

function ShiftRow({ shift, onStop }: { shift: NightShift; onStop: () => void }) {
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState<string | null>(null);
  const toggle = async () => {
    setOpen((v) => !v);
    if (!report) {
      try {
        setReport(await getNightShiftReport(shift.id));
      } catch (e) {
        setReport(e instanceof Error ? e.message : String(e));
      }
    }
  };
  const live = shift.state === 'queued' || shift.state === 'running';
  return (
    <div className="fs-wk__job" data-testid="night-shift-row">
      <div className="fs-wk__head">
        <button type="button" className="fs-wk__toggle" onClick={() => void toggle()} aria-expanded={open}>
          {open ? <ChevronDown size={14} aria-hidden="true" className="fs-wk__chev" /> : <ChevronRight size={14} aria-hidden="true" className="fs-wk__chev" />}
          <span className="fs-wk__muted">{shift.id}</span>
          <span data-testid="night-shift-state">{t(STATE_WORD[shift.state])}</span>
          <span className="fs-wk__muted">
            {shift.results.length}/{shift.tasks.length} {t('tasks')}
          </span>
        </button>
        {live && <Button variant="ghost" size="sm" icon={Square} label={t('Stop')} onClick={onStop} testId="night-shift-stop" />}
      </div>
      {open && report && <pre className="fs-wk__err-detail">{report}</pre>}
    </div>
  );
}

export function NightShiftSection({ defaultWorkspace }: { defaultWorkspace: string }) {
  const [shifts, setShifts] = useState<NightShift[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tasksText, setTasksText] = useState('');
  const [maxMinutes, setMaxMinutes] = useState(120);
  const [maxTasks, setMaxTasks] = useState(8);
  const [verify, setVerify] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setShifts(await listNightShifts());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const start = async () => {
    const tasks = tasksText.split('\n').map((t2) => t2.trim()).filter(Boolean);
    if (!tasks.length || !defaultWorkspace.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await startNightShift({
        tasks,
        workspace: defaultWorkspace.trim(),
        budget: { max_minutes: maxMinutes, max_tasks: maxTasks },
        verify,
      });
      setTasksText('');
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const stop = async (id: string) => {
    try {
      await stopNightShift(id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="fs-wk__section" data-testid="night-shift">
      <div className="fs-agents__intro">
        <p className="fs-prose">
          <Moon size={14} aria-hidden="true" /> {t('Night shift: queue tasks to run unattended, one after another, under a budget — then read the morning report.')}
        </p>
      </div>
      <div className="fs-wk__row">
        <textarea
          className="fs-field"
          rows={3}
          placeholder={t('One task per line, up to 12')}
          value={tasksText}
          onChange={(e) => setTasksText(e.target.value)}
          data-testid="night-shift-tasks"
          style={{ flex: 1 }}
        />
      </div>
      <div className="fs-wk__row">
        <label className="fs-wk__field fs-wk__field--xs">
          <span>{t('Max minutes')}</span>
          <input type="number" className="fs-field" min={1} value={maxMinutes} onChange={(e) => setMaxMinutes(parseInt(e.target.value, 10) || 1)} />
        </label>
        <label className="fs-wk__field fs-wk__field--xs">
          <span>{t('Max tasks')}</span>
          <input type="number" className="fs-field" min={1} max={12} value={maxTasks} onChange={(e) => setMaxTasks(parseInt(e.target.value, 10) || 1)} />
        </label>
        <label className="fs-switch" title={t('Verify each task\'s work before moving on')}>
          <input type="checkbox" checked={verify} onChange={(e) => setVerify(e.target.checked)} />
          <span>{t('verify')}</span>
        </label>
        <Button
          variant="primary"
          size="sm"
          icon={Moon}
          label={busy ? t('Queuing…') : t('Queue night shift')}
          loading={busy}
          disabled={!tasksText.trim() || !defaultWorkspace.trim()}
          onClick={() => void start()}
          testId="night-shift-start"
        />
      </div>
      {error && <div className="fs-wk__error">{error}</div>}
      {shifts === null ? (
        <Skeleton label={t('Loading night shifts')} height="32px" count={1} />
      ) : shifts.length === 0 ? (
        <p className="fs-wk__muted">{t('No night shift yet.')}</p>
      ) : (
        <div className="fs-wk__list">
          {shifts.map((s) => (
            <ShiftRow key={s.id} shift={s} onStop={() => void stop(s.id)} />
          ))}
        </div>
      )}
    </div>
  );
}
