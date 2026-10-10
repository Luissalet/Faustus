import { Download, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button } from '../../components';
import { formatJobLog, getJobLog, jobCommandText, jobLogDownloadUrl, type JobLog, type JobLogStream } from '../../adapters/cookbook';
import { t } from '../../i18n';
import type { Task } from '../../lib/cookbook/tasks';

/**
 * The durable record of one Cookbook job: the command as executed, exit
 * code, times, and the output with stderr highlighted. Everything here is a
 * read of `GET /api/cookbook/jobs/{id}/log`; the copy buttons put a header
 * (command, exit code, times) in front of the log so a pasted report is
 * self-contained, and "Download full log" is the capped file on disk.
 */
export function JobLogPanel({ task, say }: { task: Task; say: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [stream, setStream] = useState<JobLogStream>('combined');
  const [log, setLog] = useState<JobLog | null>(null);
  const [state, setState] = useState<'idle' | 'loading' | 'missing' | 'error'>('idle');

  const load = useCallback(() => {
    setState('loading');
    getJobLog(task.sessionId, stream, 500)
      .then((r) => {
        setLog(r);
        setState(r ? 'idle' : 'missing');
      })
      .catch(() => setState('error'));
  }, [task.sessionId, stream]);

  useEffect(() => {
    if (open) load();
  }, [open, load, task.status]);

  const copy = (text: string) => void navigator.clipboard.writeText(text).then(() => say(t('Copied')));

  return (
    <details className="fs-ck__joblog" data-testid="cookbook-job-log" onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>{t('Command and log')}</summary>
      {state === 'missing' && <p className="fs-muted">{t('No log was recorded for this session (it predates job logs).')}</p>}
      {state === 'error' && <p className="fs-muted">{t('Could not read the log.')}</p>}
      {log && (
        <>
          <p className="fs-muted fs-ck__joblog-meta">
            {log.job.exit_code !== undefined ? t('exit {n}', { n: String(log.job.exit_code) }) : t('no exit code yet')}
            {log.job.started_at ? ` · ${log.job.started_at}` : ''}
            {log.job.ended_at ? ` → ${log.job.ended_at}` : ''}
          </p>
          <pre className="fs-ck__quick" data-testid="cookbook-job-command">{jobCommandText(log)}</pre>
          <div className="fs-ck__joblog-bar">
            <select value={stream} onChange={(e) => setStream(e.target.value as JobLogStream)} aria-label={t('Stream')}>
              {(['combined', 'stdout', 'stderr'] as const).map((s) => (
                <option key={s} value={s} disabled={!log.available_streams.includes(s)}>{s}</option>
              ))}
            </select>
            <Button variant="secondary" size="sm" label={t('Copy command')} onClick={() => copy(jobCommandText(log))} />
            <Button variant="secondary" size="sm" label={t('Copy log')} onClick={() => copy(formatJobLog(log))} />
            <Button variant="secondary" size="sm" icon={Download} label={t('Download full log')} onClick={() => window.open(jobLogDownloadUrl(task.sessionId, stream), '_blank')} disabled={!log.stream_available} />
            <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} loading={state === 'loading'} onClick={load} />
          </div>
          {!log.stream_available ? (
            <p className="fs-muted">{t('This stream was not captured for this job (remote sessions only keep the combined output).')}</p>
          ) : (
            <pre className="fs-ck__output" data-testid="cookbook-job-log-lines">
              {log.truncated && <span className="fs-muted">{t('… older output omitted — download the full log.')}{'\n'}</span>}
              {log.lines.map((l, i) => (
                <span key={i} className={l.stderr ? 'fs-ck__stderr' : undefined} data-stderr={l.stderr || undefined}>{l.text}{'\n'}</span>
              ))}
            </pre>
          )}
        </>
      )}
    </details>
  );
}
