import { useCallback, useEffect, useMemo, useState, type DragEvent } from 'react';
import { Download, LayoutGrid, Pencil, Plus, RefreshCw, Table2, Upload } from 'lucide-react';
import { Button, EmptyState, IconButton, Skeleton, Toast } from '../../components';
import {
  BOARD_REFRESH_EVENT,
  BOARD_COLUMNS,
  filterIssues,
  getSummary,
  isValidBoardKey,
  ISSUE_PRIORITIES,
  ISSUE_TYPES,
  listIssues,
  canTransition,
  columnOf,
  exportMdUrl,
  groupIssuesByColumn,
  setBoardKey,
  updateIssue,
  type IssueCompact,
  type IssuePriority,
  type IssueStatus,
  type IssueType,
} from '../../adapters/board';
import { IssueCard, IssueRow, typeLabel, priorityLabel } from './IssueCard';
import { NewIssueDialog } from './NewIssueDialog';
import { IssueDetail } from './IssueDetail';
import { ImportDialog } from './ImportDialog';
import { t } from '../../i18n';
import '../board.css';

/** The status a column's own drop target sets — "closed" folds two statuses,
 *  so dropping into it needs one concrete pick; `wontfix` is the more
 *  common of the two (a `duplicate_of` link is what actually names the
 *  duplicate, made from the issue's own detail instead). */
const COLUMN_DROP_STATUS: Record<string, IssueStatus> = {
  open: 'open',
  in_progress: 'in_progress',
  blocked: 'blocked',
  done: 'done',
  closed: 'wontfix',
};

function columnLabel(id: string): string {
  return t(BOARD_COLUMNS.find((c) => c.id === id)?.label ?? id);
}

/**
 * Lote 93 (OBJ-6) — the project's work board, full view: kanban (native
 * HTML5 drag & drop between columns) and a dense sortable Table, a text/
 * type/priority/assignee filter, "New issue", "Import" (dry-run preview
 * first) and "Export .md". Mounted by `Project.tsx`'s "Board" tab.
 */
export function BoardPanel({ projectId }: { projectId: string }) {
  const [issues, setIssues] = useState<IssueCompact[] | null>(null);
  const [boardKey, setBoardKeyState] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<'kanban' | 'table'>('kanban');
  const [folded, setFolded] = useState<Record<string, boolean>>({ closed: true });
  const [filterText, setFilterText] = useState('');
  const [filterType, setFilterType] = useState<IssueType | ''>('');
  const [filterPriority, setFilterPriority] = useState<IssuePriority | ''>('');
  const [dragOverColumn, setDragOverColumn] = useState<string | null>(null);
  const [selectedIssueId, setSelectedIssueId] = useState<string | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [editingKey, setEditingKey] = useState(false);
  const [keyDraft, setKeyDraft] = useState('');
  const [keyBusy, setKeyBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [sort, setSort] = useState<{ field: keyof IssueCompact; dir: 1 | -1 }>({ field: 'updated_at', dir: -1 });

  const say = (text: string) => setToast(text);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [summary, list] = await Promise.all([
        getSummary(projectId),
        listIssues(projectId, { limit: 500 }),
      ]);
      setBoardKeyState(summary.key);
      setIssues(list.issues);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const onPing = () => void load();
    window.addEventListener(BOARD_REFRESH_EVENT, onPing);
    return () => window.removeEventListener(BOARD_REFRESH_EVENT, onPing);
  }, [load]);

  const filtered = useMemo(
    () => filterIssues(issues ?? [], { text: filterText, type: filterType || undefined, priority: filterPriority || undefined }),
    [issues, filterText, filterType, filterPriority],
  );
  const grouped = useMemo(() => groupIssuesByColumn(filtered), [filtered]);

  const patchStatus = async (issue: IssueCompact, status: IssueStatus) => {
    if (!canTransition(issue.status, status)) {
      say(t('{id} cannot move straight from {from} to {to}.', { id: issue.id, from: issue.status, to: status }));
      return;
    }
    // Optimistic: the card moves immediately, corrected if the server disagrees.
    setIssues((cur) => (cur ? cur.map((i) => (i.id === issue.id ? { ...i, status } : i)) : cur));
    try {
      const res = await updateIssue(projectId, issue.id, { status });
      setIssues((cur) => (cur ? cur.map((i) => (i.id === issue.id ? res.issue : i)) : cur));
    } catch (e) {
      say((e as Error).message);
      void load();
    }
  };

  const onDrop = (columnId: string) => (e: DragEvent) => {
    e.preventDefault();
    setDragOverColumn(null);
    const id = e.dataTransfer.getData('text/plain');
    const issue = (issues ?? []).find((i) => i.id === id);
    if (!issue) return;
    const status = COLUMN_DROP_STATUS[columnId];
    if (status && columnOf(issue.status) !== columnId) void patchStatus(issue, status);
  };

  const saveKey = async () => {
    if (!isValidBoardKey(keyDraft)) return;
    setKeyBusy(true);
    try {
      const res = await setBoardKey(projectId, keyDraft);
      setBoardKeyState(res.key);
      setEditingKey(false);
      say(t('New issues will use {key}-N. Existing ids keep their old key.', { key: res.key }));
    } catch (e) {
      say((e as Error).message);
    } finally {
      setKeyBusy(false);
    }
  };

  const sortedTable = useMemo(() => {
    const rows = [...filtered];
    rows.sort((a, b) => {
      const av = a[sort.field], bv = b[sort.field];
      const cmp = String(av).localeCompare(String(bv));
      return cmp * sort.dir;
    });
    return rows;
  }, [filtered, sort]);

  const toggleSort = (field: keyof IssueCompact) =>
    setSort((cur) => (cur.field === field ? { field, dir: cur.dir === 1 ? -1 : 1 } : { field, dir: 1 }));

  if (loading && issues === null) {
    return <Skeleton label={t('Loading the board')} count={4} height="120px" />;
  }
  if (error && issues === null) {
    return <EmptyState tone="error" title={t('Could not load the board')} body={error} primaryAction={{ label: t('Retry'), onClick: () => void load() }} />;
  }

  return (
    <div className="fs-board" data-testid="board-panel">
      <div className="fs-board__toolbar">
        <input
          className="fs-field fs-board__search"
          value={filterText}
          onChange={(e) => setFilterText(e.target.value)}
          placeholder={t('Filter by text or id…')}
          data-testid="board-filter-text"
        />
        <select className="fs-field" value={filterType} onChange={(e) => setFilterType(e.target.value as IssueType | '')} aria-label={t('Type')}>
          <option value="">{t('Any type')}</option>
          {ISSUE_TYPES.map((ty) => <option key={ty} value={ty}>{typeLabel(ty)}</option>)}
        </select>
        <select className="fs-field" value={filterPriority} onChange={(e) => setFilterPriority(e.target.value as IssuePriority | '')} aria-label={t('Priority')}>
          <option value="">{t('Any priority')}</option>
          {ISSUE_PRIORITIES.map((p) => <option key={p} value={p}>{priorityLabel(p)}</option>)}
        </select>
        <span className="fs-board__toolbar-spacer" />
        <Button
          variant="ghost"
          size="sm"
          icon={view === 'kanban' ? Table2 : LayoutGrid}
          label={view === 'kanban' ? t('Table') : t('Kanban')}
          onClick={() => setView((v) => (v === 'kanban' ? 'table' : 'kanban'))}
          testId="board-view-toggle"
        />
        <Button variant="ghost" size="sm" icon={Upload} label={t('Import…')} onClick={() => setImportOpen(true)} testId="board-import-open" />
        <a className="fs-btn" data-variant="ghost" data-size="sm" href={exportMdUrl(projectId)} target="_blank" rel="noreferrer" data-testid="board-export-link">
          <Download size={16} aria-hidden="true" /> <span>{t('Export .md')}</span>
        </a>
        <Button variant="secondary" size="sm" icon={Plus} label={t('New issue')} onClick={() => setNewOpen(true)} testId="board-new-issue-open" />
      </div>

      <div className="fs-board__key">
        {editingKey ? (
          <form
            className="fs-inline"
            onSubmit={(e) => { e.preventDefault(); void saveKey(); }}
          >
            <input
              className="fs-field"
              value={keyDraft}
              onChange={(e) => setKeyDraft(e.target.value.toUpperCase())}
              placeholder="FAU"
              maxLength={5}
              autoFocus
              data-testid="board-key-input"
            />
            <Button type="submit" size="sm" label={t('Save')} disabled={!isValidBoardKey(keyDraft)} loading={keyBusy} testId="board-key-save" />
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setEditingKey(false)} />
          </form>
        ) : (
          <button
            type="button"
            className="fs-board__key-chip"
            onClick={() => { setKeyDraft(boardKey); setEditingKey(true); }}
            title={t('Ids for new issues start with this key. Changing it does not renumber existing ones.')}
            data-testid="board-key-edit"
          >
            {t('Key: {key}', { key: boardKey })} <Pencil size={11} aria-hidden="true" />
          </button>
        )}
        <IconButton icon={RefreshCw} label={t('Refresh')} size="sm" onClick={() => void load()} testId="board-refresh" />
      </div>

      {issues && issues.length === 0 ? (
        <EmptyState
          icon={LayoutGrid}
          title={t('The board is empty#issue_board')}
          body={t('Log a bug, an idea or a task, or import what is already written down in OBJETIVOS.md, PENDIENTES.md or the backlog.')}
          primaryAction={{ label: t('New issue'), onClick: () => setNewOpen(true) }}
          secondaryAction={{ label: t('Import…'), onClick: () => setImportOpen(true) }}
        />
      ) : view === 'kanban' ? (
        <div className="fs-board__columns">
          {BOARD_COLUMNS.map((col) => {
            const items = grouped[col.id] ?? [];
            const isFolded = col.foldedByDefault && folded[col.id] !== false;
            return (
              <section
                key={col.id}
                className="fs-board__column"
                data-folded={isFolded || undefined}
                data-drag-over={dragOverColumn === col.id || undefined}
                onDragOver={(e) => { e.preventDefault(); setDragOverColumn(col.id); }}
                onDragLeave={() => setDragOverColumn((c) => (c === col.id ? null : c))}
                onDrop={onDrop(col.id)}
              >
                <button
                  type="button"
                  className="fs-board__column-head"
                  onClick={() => col.foldedByDefault && setFolded((f) => ({ ...f, [col.id]: !isFolded }))}
                  aria-expanded={!isFolded}
                >
                  <span>{columnLabel(col.id)}</span>
                  <span className="fs-board__column-count">{items.length}</span>
                </button>
                {!isFolded && (
                  <div className="fs-board__column-body">
                    {items.length === 0 && <p className="fs-board__column-empty">{t('Nothing here')}</p>}
                    {items.map((issue) => (
                      <IssueCard key={issue.id} issue={issue} onOpen={(i) => setSelectedIssueId(i.id)} />
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      ) : (
        <div className="fs-issue-table-wrap" role="region" aria-label={t('Issues table')} tabIndex={0}>
          <table className="fs-issue-table">
            <thead>
              <tr>
                {(['id', 'type', 'title', 'priority', 'status', 'assignee', 'labels'] as (keyof IssueCompact)[]).map((field) => (
                  <th key={field}>
                    <button type="button" onClick={() => toggleSort(field)} data-testid={`board-sort-${field}`}>
                      {field} {sort.field === field ? (sort.dir === 1 ? '▲' : '▼') : ''}
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sortedTable.map((issue) => (
                <IssueRow key={issue.id} issue={issue} onOpen={(i) => setSelectedIssueId(i.id)} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <NewIssueDialog
        open={newOpen}
        onOpenChange={setNewOpen}
        projectId={projectId}
        onCreated={(issue) => {
          setIssues((cur) => (cur ? [...cur, issue] : [issue]));
          say(t('Filed as {id}.', { id: issue.id }));
        }}
      />
      <ImportDialog open={importOpen} onOpenChange={setImportOpen} projectId={projectId} onImported={() => void load()} />
      <IssueDetail
        open={selectedIssueId !== null}
        onOpenChange={(o) => !o && setSelectedIssueId(null)}
        projectId={projectId}
        issueId={selectedIssueId}
        onChanged={(issue) => setIssues((cur) => (cur ? cur.map((i) => (i.id === issue.id ? issue : i)) : cur))}
        onDeleted={(id) => {
          setIssues((cur) => (cur ? cur.filter((i) => i.id !== id) : cur));
          setSelectedIssueId(null);
        }}
      />
      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
