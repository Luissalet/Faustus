import { GitBranch, Plus, Search } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Popover } from '../../components';
import {
  checkout,
  filterBranches,
  getBranches,
  GitApiError,
  type GitBranchesResponse,
  type GitRepo,
} from '../../adapters/git';
import { CreateBranchDialog } from './CreateBranchDialog';
import { t } from '../../i18n';

/**
 * VS Code's branch switcher: click the current branch chip, get a
 * searchable list of local + remote branches plus "New branch…", which
 * opens `CreateBranchDialog` (lote 83: name + "from" + "switch to it",
 * replacing the plain inline text field this used to have). Branches load
 * lazily on open (`Popover`'s `onOpenChange`, lote 81's additive change to
 * the shared component) rather than for every repo row up front — with up
 * to 60 repos discovered per owner, eagerly calling `/branches` for all of
 * them would be 60 requests nobody asked for yet.
 *
 * A dirty checkout (409 `git.dirty`) is shown right here, with the exact
 * files git named, rather than closing the popover on a generic toast —
 * the person needs to see what to stash or commit before trying again.
 */
export function BranchPopover({
  repoId,
  currentBranch,
  detached,
  onCheckedOut,
}: {
  repoId: string;
  currentBranch: string | null;
  detached: boolean;
  onCheckedOut: (repo: GitRepo) => void;
}) {
  const [open, setOpen] = useState(false);
  const [branches, setBranches] = useState<GitBranchesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<{ message: string; dirty: string[] } | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    getBranches(repoId)
      .then(setBranches)
      .catch((e: unknown) => setError({ message: (e as Error).message, dirty: [] }))
      .finally(() => setLoading(false));
  }, [open, repoId]);

  const reset = () => {
    setOpen(false);
    setError(null);
  };

  const doCheckout = async (branch: string, create = false, startPoint?: string) => {
    setBusy(branch);
    setError(null);
    try {
      const result = await checkout(repoId, { branch, create, startPoint });
      onCheckedOut(result.repo);
      reset();
    } catch (e) {
      if (e instanceof GitApiError && e.errorClass === 'git.dirty') {
        const dirty = Array.isArray(e.payload.dirty)
          ? (e.payload.dirty as unknown[]).filter((p): p is string => typeof p === 'string')
          : [];
        setError({ message: e.message, dirty });
      } else {
        setError({ message: (e as Error).message, dirty: [] });
      }
    } finally {
      setBusy(null);
    }
  };

  const local = branches ? filterBranches(branches.local, search) : [];
  const remote = branches ? filterBranches(branches.remote, search) : [];
  const noMatches = branches !== null && local.length === 0 && remote.length === 0;

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) reset();
      }}
      className="fs-sc__branch-pop"
      testId="branch-popover"
      trigger={
        <button
          type="button"
          className="fs-chip"
          data-on={open || undefined}
          aria-label={t('Change branch — currently {branch}', {
            branch: detached ? t('detached HEAD') : currentBranch ?? t('no branch'),
          })}
          data-testid="repo-branch-chip"
        >
          <GitBranch size={13} aria-hidden="true" />
          {detached ? t('detached') : currentBranch ?? t('no branch')}
        </button>
      }
    >
      <div className="fs-sc__branch-search">
        <Search size={13} aria-hidden="true" />
        <input
          className="fs-field"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('Find a branch…')}
          aria-label={t('Find a branch')}
          data-testid="branch-search"
        />
      </div>

      {loading && <p className="fs-muted">{t('Loading branches…')}</p>}

      {error && (
        <div className="fs-notice" data-tone="danger" role="alert" data-testid="branch-checkout-error">
          <p>{error.message}</p>
          {error.dirty.length > 0 && (
            <ul className="fs-sc__dirty-list">
              {error.dirty.map((path) => (
                <li key={path}>{path}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!loading && branches && (
        <div className="fs-sc__branch-list" role="list" aria-label={t('Branches')}>
          {local.map((b) => (
            <button
              key={`local-${b.name}`}
              type="button"
              role="listitem"
              className="fs-sc__branch-row"
              data-current={b.is_current || undefined}
              disabled={busy !== null || b.is_current}
              onClick={() => void doCheckout(b.name)}
              data-testid="branch-row-local"
            >
              <GitBranch size={13} aria-hidden="true" />
              <span>{b.name}</span>
              {b.is_current && <span className="fs-sc__branch-tag">{t('current')}</span>}
            </button>
          ))}
          {remote.map((b) => (
            <button
              key={`remote-${b.name}`}
              type="button"
              role="listitem"
              className="fs-sc__branch-row"
              disabled={busy !== null}
              onClick={() => void doCheckout(b.name.replace(/^[^/]+\//, ''), true, b.name)}
              data-testid="branch-row-remote"
            >
              <GitBranch size={13} aria-hidden="true" />
              <span>{b.name}</span>
              <span className="fs-sc__branch-tag">{t('remote')}</span>
            </button>
          ))}
          {noMatches && <p className="fs-muted">{t('No branches match “{query}”.', { query: search })}</p>}
        </div>
      )}

      <div className="fs-sc__branch-create">
        <Button size="sm" variant="ghost" icon={Plus} label={t('New branch…')} onClick={() => setCreateOpen(true)} testId="branch-create-open" />
      </div>

      <CreateBranchDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        repoId={repoId}
        branches={branches}
        onCreated={(updated) => {
          onCheckedOut(updated);
          reset();
        }}
      />
    </Popover>
  );
}
