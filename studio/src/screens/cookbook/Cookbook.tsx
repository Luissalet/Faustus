import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { loadState, useCookbookState, type HwSystem } from '../../adapters/cookbook';
import type { ServeFields } from '../../lib/cookbook/serve';
import type { Task } from '../../lib/cookbook/tasks';
import { t, tn } from '../../i18n';
import { Dependencies } from './Dependencies';
import { Download } from './Download';
import { Fit } from './Fit';
import { Models } from './Models';
import { useTaskMonitor } from './monitor';
import { Notice, ServerPicker, useSay, useSelectedServer } from './parts';
import { Running } from './Running';
import { ScheduleDialog } from './Schedule';
import { Servers } from './Servers';
import '../projects.css';
import '../cookbook.css';

/**
 * Cookbook: the kitchen for local models. What fits this machine, what is
 * already here, pulling more, launching an engine on it, watching the
 * sessions, the dependencies each engine needs, and the servers all of
 * that runs on. `?t=fit|models|download|running|deps|servers`;
 * `?repo=<id>` opens a model's launch form, `?pkg=<name>` a dependency.
 *
 * The previous interface kept this in a modal with four tabs and a
 * "Running" badge in the sidebar; here every part is a URL and the
 * monitor runs while the screen is open.
 */

type Tab = 'fit' | 'models' | 'download' | 'running' | 'deps' | 'servers';
const TABS: { key: Tab; label: string; hint: string }[] = [
  { key: 'fit', label: 'Fit', hint: 'The catalogue ranked against this hardware' },
  { key: 'models', label: 'Models', hint: 'What is cached here, ready to launch' },
  { key: 'download', label: 'Download', hint: 'Pull a repo, a GGUF or an Ollama tag' },
  { key: 'running', label: 'Running', hint: 'Every session, its output and what went wrong' },
  { key: 'deps', label: 'Dependencies', hint: 'What each engine needs on this server' },
  { key: 'servers', label: 'Servers', hint: 'This machine and the SSH boxes' },
];

export function CookbookScreen() {
  const [params, setParams] = useSearchParams();
  const raw = params.get('t');
  const tab: Tab = (TABS.some((x) => x.key === raw) ? raw : 'fit') as Tab;
  const state = useCookbookState();
  const server = useSelectedServer();
  const [notice, say] = useSay();
  const [system, setSystem] = useState<HwSystem | null>(null);
  const [edit, setEdit] = useState<{ repo: string; fields?: ServeFields; replaceTaskId?: string; focus?: string } | null>(null);
  const [schedule, setSchedule] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    loadState()
      .catch(() => null)
      .finally(() => setLoaded(true));
  }, []);
  useTaskMonitor(say, loaded);

  const go = useCallback(
    (next: Tab, extra?: Record<string, string>) => {
      const p = new URLSearchParams();
      p.set('t', next);
      if (extra) for (const [k, v] of Object.entries(extra)) if (v) p.set(k, v);
      setParams(p);
    },
    [setParams],
  );

  const live = useMemo(() => state.tasks.filter((x) => x.status === 'running' || x.status === 'ready' || x.status === 'queued').length, [state.tasks]);
  const failed = useMemo(() => state.tasks.filter((x) => x.status === 'error' || x.status === 'crashed').length, [state.tasks]);
  const hwBackend = system?.backend ?? '';

  const editTask = (task: Task, overrides?: Record<string, string>) => {
    const fields: ServeFields = { ...(task.payload?._fields ?? {}) };
    let focus: string | undefined;
    for (const [k, v] of Object.entries(overrides ?? {})) {
      if (k === '_focus') focus = v;
      else if (k !== '_fromDownload') fields[k] = v;
    }
    setEdit({ repo: task.payload?.repo_id || task.name, fields, replaceTaskId: overrides?._fromDownload ? undefined : task.sessionId, focus });
    go('models', { repo: task.payload?.repo_id || task.name });
  };

  return (
    <div className="fs-screen fs-ck" data-testid="cookbook">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Cookbook')}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {t('What fits this machine, what is already here, and the engines that serve it.')}
            {live ? ` ${tn(live, '{n} session live.', '{n} sessions live.')}` : ''}
          </p>
        </div>
        <ServerPicker />
      </header>
      <div className="fs-tabs" role="tablist" aria-label={t('Cookbook')}>
        {TABS.map((entry) => (
          <button key={entry.key} type="button" role="tab" className="fs-tab" aria-selected={tab === entry.key} title={t(entry.hint)} onClick={() => go(entry.key)} data-testid={`cookbook-tab-${entry.key}`}>
            {t(entry.label)}
            {entry.key === 'running' && (live || failed) ? (
              <span className="fs-ck__badge" data-tone={failed ? 'danger' : undefined}>
                {failed || live}
              </span>
            ) : null}
          </button>
        ))}
      </div>
      <div className="fs-ck__panel-host" role="tabpanel">
        {tab === 'fit' && <Fit server={server} hwBackend={hwBackend} onSystem={setSystem} say={say} onCached={(repo) => go('models', { repo })} />}
        {tab === 'models' && (
          <Models
            server={server}
            hwBackend={hwBackend}
            say={say}
            openRepo={params.get('repo')}
            edit={edit}
            onLaunched={() => {
              setEdit(null);
              go('running');
            }}
            onSchedule={(repo) => setSchedule(repo)}
            onDownloadTab={() => go('download')}
          />
        )}
        {tab === 'download' && <Download server={server} say={say} prefill={params.get('repo')} onStarted={() => go('running')} />}
        {tab === 'running' && <Running say={say} hwBackend={hwBackend} onEdit={editTask} onDeps={(pkg) => go('deps', { pkg })} />}
        {tab === 'deps' && <Dependencies server={server} hwBackend={hwBackend} say={say} highlight={params.get('pkg')} onTask={() => go('running')} />}
        {tab === 'servers' && <Servers say={say} />}
      </div>
      {schedule && <ScheduleDialog repo={schedule} host={server?.host ?? ''} onClose={() => setSchedule(null)} say={say} />}
      <Notice text={notice} />
    </div>
  );
}
