import { FolderPlus, Radar, RefreshCw, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { Button, Dialog, IconButton, Skeleton, Toast } from '../../components';
import { relativeTime } from '../../adapters/home';
import {
  getRadar,
  getWatchRoots,
  GIT_REFRESH_EVENT,
  putWatchRoots,
  radarReasonLabel,
  type GitRadar,
  type GitRadarReason,
  type GitRadarRepo,
} from '../../adapters/git';
import { t, tn } from '../../i18n';

const POLL_MS = 60000;

/**
 * The git radar (src/git_radar.py): every repository Faustus can see —
 * project links plus the watched folders — that still has work on this
 * machine only: uncommitted files, commits not pushed, a branch with no
 * upstream, a repo with no remote at all. The thing you forget when a
 * change was made from a chat and the tab was closed.
 *
 * `useGitRadar` is the one poll (60 s, only while visible, plus the
 * panel's own `GIT_REFRESH_EVENT` after any commit/push); `RadarRows`
 * draws the attention list and is reused by Home's block and by the strip
 * above the Source control panel.
 */
export function useGitRadar(): { radar: GitRadar | null; error: string | null; reload: (refresh?: boolean) => void; loading: boolean } {
  const [radar, setRadar] = useState<GitRadar | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const alive = useRef(true);

  const reload = useCallback((refresh = false) => {
    setLoading(true);
    getRadar({ refresh })
      .then((r) => {
        if (!alive.current) return;
        setRadar(r);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!alive.current) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    alive.current = true;
    reload();
    const tick = () => {
      if (document.visibilityState === 'visible') reload();
    };
    const timer = window.setInterval(tick, POLL_MS);
    const onRefresh = () => reload(true);
    window.addEventListener(GIT_REFRESH_EVENT, onRefresh);
    document.addEventListener('visibilitychange', tick);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      window.removeEventListener(GIT_REFRESH_EVENT, onRefresh);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [reload]);

  return { radar, error, reload, loading };
}

function ReasonChip({ reason }: { reason: GitRadarReason }) {
  const label = radarReasonLabel(reason);
  const tone = reason.kind === 'conflicts' ? 'danger'
    : reason.kind === 'behind' || reason.kind === 'detached' ? 'info'
      : 'warning';
  return (
    <span className="fs-radar__chip" data-kind={reason.kind} data-tone={tone}>
      {t(label.key, label.values)}
    </span>
  );
}

function repoWhere(repo: GitRadarRepo): string {
  const names = repo.projects.map((p) => p.name).filter((n): n is string => Boolean(n));
  if (names.length > 0) return [...new Set(names)].join(' · ');
  if (repo.watched && repo.root_folder) {
    const base = repo.root_folder.replace(/[\\/]+$/, '').split(/[\\/]/).pop();
    return base ? t('Watched: {folder}', { folder: base }) : t('Watched folder');
  }
  return '';
}

/** The attention rows, each a link into Source control with that repo
 *  selected. `limit` keeps Home short; the strip shows them all. */
export function RadarRows({ repos, limit }: { repos: GitRadarRepo[]; limit?: number }) {
  const shown = limit ? repos.slice(0, limit) : repos;
  return (
    <div className="fs-list fs-list--rail fs-radar__list" role="list" aria-label={t('Repositories needing attention')}>
      {shown.map((repo) => {
        const where = repoWhere(repo);
        const age = repo.last_commit_at ? relativeTime(new Date(repo.last_commit_at * 1000).toISOString()) : '';
        return (
          <Link
            key={repo.id}
            role="listitem"
            className="fs-row fs-radar__row"
            to={`/source-control?repo=${encodeURIComponent(repo.id)}`}
            data-testid="radar-row"
            title={repo.path}
          >
            <span className="fs-row__main">
              <span className="fs-row__name">
                {repo.name}
                {repo.branch && <span className="fs-radar__branch">{repo.branch}</span>}
              </span>
              <span className="fs-row__meta">
                {[where, age ? t('last commit {when}', { when: age }) : null].filter(Boolean).join(' · ')}
              </span>
            </span>
            <span className="fs-radar__chips">
              {repo.reasons.map((r) => <ReasonChip key={r.kind} reason={r} />)}
            </span>
          </Link>
        );
      })}
    </div>
  );
}

/**
 * The strip above the Source control panel: a one-line verdict ("3 of 24
 * repositories have work that has not left this machine"), the rows, a
 * refresh, and the Watched folders editor. Collapses to a single calm line
 * when everything is committed and pushed.
 */
export function GitRadarStrip() {
  const { radar, error, reload, loading } = useGitRadar();
  const [rootsOpen, setRootsOpen] = useState(false);

  return (
    <section className="fs-radar" data-testid="git-radar" aria-labelledby="git-radar-title">
      <div className="fs-radar__head">
        <h2 id="git-radar-title" className="fs-radar__title">
          <Radar size={16} aria-hidden="true" />
          {t('Needs commit or push')}
          {radar && radar.attention_count > 0 && (
            <span className="fs-sc__repo-dirty" data-testid="radar-count">{radar.attention_count}</span>
          )}
        </h2>
        <p className="fs-radar__sub">
          {error
            ? error
            : !radar
              ? t('Scanning repositories…')
              : radar.attention_count === 0
                ? tn(radar.total, 'Your {n} repository is committed and pushed.', 'All {n} repositories are committed and pushed.')
                : t('{n} of {total} repositories have work that has not left this machine.', { n: radar.attention_count, total: radar.total })}
        </p>
        <div className="fs-radar__actions">
          <Button
            variant="ghost"
            size="sm"
            icon={FolderPlus}
            label={t('Watched folders')}
            onClick={() => setRootsOpen(true)}
            testId="radar-watched-folders"
          />
          <IconButton icon={RefreshCw} label={t('Rescan')} size="sm" disabled={loading} onClick={() => reload(true)} testId="radar-rescan" />
        </div>
      </div>
      {!radar && !error && <Skeleton label={t('Scanning repositories')} count={2} height="44px" />}
      {radar && radar.attention_count > 0 && <RadarRows repos={radar.attention} />}
      <WatchedFoldersDialog
        open={rootsOpen}
        onOpenChange={setRootsOpen}
        onSaved={() => reload(true)}
      />
    </section>
  );
}

/**
 * Watched folders: the install-wide roots (`git_watch_roots`) scanned for
 * repositories on top of every project's linked folders — so a repo that
 * belongs to no Faustus project still shows in Source control and in the
 * radar. Absolute paths only; the server refuses one that does not exist.
 */
export function WatchedFoldersDialog({
  open,
  onOpenChange,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}) {
  const [roots, setRoots] = useState<string[] | null>(null);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 4000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    if (!open) return;
    let live = true;
    getWatchRoots()
      .then((r) => {
        if (live) setRoots(r.configured);
      })
      .catch((e: unknown) => {
        if (live) {
          setRoots([]);
          setToast(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      live = false;
    };
  }, [open]);

  const add = () => {
    const p = draft.trim();
    if (!p || !roots) return;
    if (roots.includes(p)) {
      setDraft('');
      return;
    }
    setRoots([...roots, p]);
    setDraft('');
  };

  const save = () => {
    if (!roots) return;
    setSaving(true);
    putWatchRoots(roots)
      .then((r) => {
        setRoots(r.watch_roots);
        setToast(t('Watched folders saved'));
        onSaved();
        onOpenChange(false);
      })
      .catch((e: unknown) => setToast(e instanceof Error ? e.message : String(e)))
      .finally(() => setSaving(false));
  };

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={onOpenChange}
        title={t('Watched folders')}
        description={t('Folders scanned for git repositories besides your linked project folders (up to three levels deep). Absolute paths.')}
        testId="watched-folders-dialog"
        footer={
          <>
            <Button variant="ghost" label={t('Cancel')} onClick={() => onOpenChange(false)} />
            <Button variant="primary" label={t('Save')} loading={saving} disabled={!roots} onClick={save} testId="watched-folders-save" />
          </>
        }
      >
        {!roots ? (
          <Skeleton label={t('Loading watched folders')} count={2} height="32px" />
        ) : (
          <div className="fs-radar__roots">
            {roots.length === 0 && <p className="fs-radar__empty">{t('No watched folders yet. Add one below.')}</p>}
            <ul className="fs-radar__root-list">
              {roots.map((r) => (
                <li key={r} className="fs-radar__root">
                  <code className="fs-radar__root-path" title={r}>{r}</code>
                  <IconButton
                    icon={Trash2}
                    label={t('Remove {path}', { path: r })}
                    size="sm"
                    onClick={() => setRoots(roots.filter((x) => x !== r))}
                    testId="watched-folder-remove"
                  />
                </li>
              ))}
            </ul>
            <form
              className="fs-radar__add"
              onSubmit={(e) => {
                e.preventDefault();
                add();
              }}
            >
              <label className="fs-radar__add-label" htmlFor="radar-root-input">{t('Add a folder')}</label>
              <input
                id="radar-root-input"
                className="fs-field fs-radar__add-input"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={t('e.g. C:/Users/you/Projects or /home/you/code')}
                spellCheck={false}
                data-testid="watched-folder-input"
              />
              <Button type="submit" variant="secondary" size="sm" icon={FolderPlus} label={t('Add')} disabled={!draft.trim()} testId="watched-folder-add" />
            </form>
          </div>
        )}
      </Dialog>
      {toast && <Toast>{toast}</Toast>}
    </>
  );
}
