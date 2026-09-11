import { Minus, Plus, Trash2, XCircle } from 'lucide-react';
import { useState, type KeyboardEvent, type ReactNode } from 'react';
import { Button, Dialog, EmptyState, IconButton, Skeleton } from '../../components';
import { fileStatusLabel, type GitStatus, type GitStatusFile, type GitUnstagedFile, type GitUser, type GitUntrackedFile } from '../../adapters/git';
import { t, tn } from '../../i18n';

export interface WorkingFileSelection {
  path: string;
  staged: boolean;
}

function FileRow({
  path,
  status,
  selected,
  onSelect,
  onAction,
  actionIcon: ActionIcon,
  actionLabel,
  busy,
  onDiscard,
}: {
  path: string;
  status: string;
  selected: boolean;
  onSelect: () => void;
  onAction: () => void;
  actionIcon: typeof Plus;
  actionLabel: string;
  busy: boolean;
  onDiscard?: () => void;
}) {
  const statusTitle = status === 'U' ? t('Untracked') : t(fileStatusLabel(status as GitStatusFile['status']));
  return (
    <div role="listitem" className="fs-sc__file-row" aria-current={selected ? 'true' : undefined}>
      <button type="button" className="fs-sc__file-main" onClick={onSelect} data-testid="working-file-row">
        <span className="fs-sc__file-status" data-status={status} title={statusTitle}>
          {status}
        </span>
        <span className="fs-sc__file-path">{path}</span>
      </button>
      <span className="fs-sc__file-row-actions">
        {onDiscard && <IconButton icon={Trash2} label={t('Discard changes in {path}', { path })} size="sm" onClick={onDiscard} disabled={busy} testId="file-discard" />}
        <IconButton icon={ActionIcon} label={actionLabel} size="sm" onClick={onAction} disabled={busy} testId="file-stage-toggle" />
      </span>
    </div>
  );
}

function Section({
  title,
  count,
  bulkIcon: BulkIcon,
  bulkLabel,
  onBulk,
  busy,
  children,
}: {
  title: string;
  count: number;
  bulkIcon?: typeof Plus;
  bulkLabel?: string;
  onBulk?: () => void;
  busy: boolean;
  children: ReactNode;
}) {
  if (count === 0) return null;
  return (
    <section className="fs-sc__section">
      <header className="fs-sc__section-head">
        <h3>
          {title} <span className="fs-sc__section-count">{count}</span>
        </h3>
        {BulkIcon && bulkLabel && onBulk && <IconButton icon={BulkIcon} label={bulkLabel} size="sm" onClick={onBulk} disabled={busy} testId="section-bulk-action" />}
      </header>
      <div role="list" aria-label={title}>
        {children}
      </div>
    </section>
  );
}

/**
 * CHANGES: the commit box (Ctrl+Enter commits, same shortcut VS Code uses)
 * and the three change sections — Staged, Changes, Untracked — each with a
 * per-file and per-section stage/unstage, plus a destructive Discard that
 * asks first (the contract makes `/discard` require an explicit
 * `{"confirm": true}` for exactly this reason).
 */
export function ChangesPane({
  status,
  loadingStatus,
  statusError,
  message,
  onMessageChange,
  onCommit,
  committing,
  commitDisabled,
  commitError,
  onStage,
  onUnstage,
  onDiscard,
  selectedFile,
  onSelectFile,
  actionBusy,
  actionError,
  user,
  onAbortMerge,
  abortingMerge = false,
}: {
  status: GitStatus | null;
  loadingStatus: boolean;
  statusError: string | null;
  message: string;
  onMessageChange: (v: string) => void;
  onCommit: () => void;
  committing: boolean;
  commitDisabled: boolean;
  commitError: string | null;
  onStage: (paths: string[] | 'all') => void;
  onUnstage: (paths: string[] | 'all') => void;
  onDiscard: (paths: string[]) => void;
  selectedFile: WorkingFileSelection | null;
  onSelectFile: (file: WorkingFileSelection) => void;
  actionBusy: boolean;
  actionError: string | null;
  user: GitUser;
  /** Lote 89: rolls a merge paused with conflicts (`keepConflicts: true`
   *  from `MergeDialog`) all the way back. Optional so older callers of
   *  this pane keep compiling — the button only renders when both this and
   *  `status.conflicts` are present. */
  onAbortMerge?: () => void;
  abortingMerge?: boolean;
}) {
  const [discardTarget, setDiscardTarget] = useState<string | null>(null);

  const onMessageKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      if (!commitDisabled) onCommit();
    }
  };

  if (loadingStatus) {
    return <Skeleton label={t('Loading changes')} count={4} height="32px" />;
  }
  if (statusError) {
    return (
      <p className="fs-notice" data-tone="danger" role="alert">
        {statusError}
      </p>
    );
  }
  if (!status) return null;

  const nothingToShow = status.staged.length === 0 && status.unstaged.length === 0 && status.untracked.length === 0 && status.conflicts.length === 0;

  return (
    <div className="fs-sc__changes">
      <form
        className="fs-sc__commit-box"
        onSubmit={(e) => {
          e.preventDefault();
          if (!commitDisabled) onCommit();
        }}
      >
        <label className="fs-sc__commit-label" htmlFor="fs-sc-commit-message">
          {t('Commit message ({name})', { name: user.name || t('no git identity configured') })}
        </label>
        <textarea
          id="fs-sc-commit-message"
          className="fs-field fs-sc__commit-input"
          rows={3}
          value={message}
          onChange={(e) => onMessageChange(e.target.value)}
          onKeyDown={onMessageKey}
          placeholder={t('Message (Ctrl+Enter to commit)')}
          data-testid="commit-message"
        />
        <div className="fs-inline">
          <Button type="submit" variant="primary" size="sm" label={t('Commit')} loading={committing} disabled={commitDisabled} testId="commit-submit" />
          <span className="fs-muted">{tn(status.staged.length, '{n} file staged', '{n} files staged')}</span>
        </div>
        {commitError && (
          <p className="fs-notice" data-tone="danger" role="alert" data-testid="commit-error">
            {commitError}
          </p>
        )}
      </form>

      {actionError && (
        <p className="fs-notice" data-tone="danger" role="alert" data-testid="action-error">
          {actionError}
        </p>
      )}

      {nothingToShow && (
        <EmptyState headingLevel={3} title={t('No changes')} body={t('The working tree is clean — nothing to stage or commit.')} />
      )}

      <Section
        title={t('Merge Conflicts')}
        count={status.conflicts.length}
        bulkIcon={onAbortMerge ? XCircle : undefined}
        bulkLabel={onAbortMerge ? t('Abort merge') : undefined}
        onBulk={onAbortMerge}
        busy={actionBusy || abortingMerge}
      >
        {status.conflicts.map((f: GitUntrackedFile) => (
          <div role="listitem" className="fs-sc__file-row" key={f.path}>
            <span className="fs-sc__file-status" data-status="conflict">!</span>
            <span className="fs-sc__file-path">{f.path}</span>
          </div>
        ))}
      </Section>

      <Section
        title={t('Staged Changes')}
        count={status.staged.length}
        bulkIcon={Minus}
        bulkLabel={t('Unstage all')}
        onBulk={() => onUnstage('all')}
        busy={actionBusy}
      >
        {status.staged.map((f: GitStatusFile) => (
          <FileRow
            key={f.path}
            path={f.path}
            status={f.status}
            selected={selectedFile?.path === f.path && selectedFile.staged}
            onSelect={() => onSelectFile({ path: f.path, staged: true })}
            onAction={() => onUnstage([f.path])}
            actionIcon={Minus}
            actionLabel={t('Unstage {path}', { path: f.path })}
            busy={actionBusy}
          />
        ))}
      </Section>

      <Section
        title={t('Changes')}
        count={status.unstaged.length}
        bulkIcon={Plus}
        bulkLabel={t('Stage all')}
        onBulk={() => onStage('all')}
        busy={actionBusy}
      >
        {status.unstaged.map((f: GitUnstagedFile) => (
          <FileRow
            key={f.path}
            path={f.path}
            status={f.status}
            selected={selectedFile?.path === f.path && !selectedFile.staged}
            onSelect={() => onSelectFile({ path: f.path, staged: false })}
            onAction={() => onStage([f.path])}
            actionIcon={Plus}
            actionLabel={t('Stage {path}', { path: f.path })}
            busy={actionBusy}
            onDiscard={() => setDiscardTarget(f.path)}
          />
        ))}
      </Section>

      <Section
        title={t('Untracked Changes')}
        count={status.untracked.length}
        bulkIcon={Plus}
        bulkLabel={t('Stage all')}
        onBulk={() => onStage('all')}
        busy={actionBusy}
      >
        {status.untracked.map((f: GitUntrackedFile) => (
          <FileRow
            key={f.path}
            path={f.path}
            status="U"
            selected={selectedFile?.path === f.path && !selectedFile.staged}
            onSelect={() => onSelectFile({ path: f.path, staged: false })}
            onAction={() => onStage([f.path])}
            actionIcon={Plus}
            actionLabel={t('Stage {path}', { path: f.path })}
            busy={actionBusy}
          />
        ))}
      </Section>

      <Dialog
        open={discardTarget !== null}
        onOpenChange={(open) => { if (!open) setDiscardTarget(null); }}
        title={t('Discard changes?')}
        description={discardTarget ?? undefined}
        testId="discard-confirm"
        footer={
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setDiscardTarget(null)} />
            <Button
              variant="danger-solid"
              size="sm"
              label={t('Discard')}
              loading={actionBusy}
              onClick={() => {
                if (discardTarget) onDiscard([discardTarget]);
                setDiscardTarget(null);
              }}
              testId="discard-confirm-ok"
            />
          </>
        }
      >
        <p className="fs-prose">{t('This cannot be undone: the file goes back to its last committed content.')}</p>
      </Dialog>
    </div>
  );
}
