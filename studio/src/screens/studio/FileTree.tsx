import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Eye, File as FileIcon, Folder, FolderOpen, Pencil } from 'lucide-react';
import { basename, browseWorkspace, type BrowseResult } from '../../adapters/composer';
import { t } from '../../i18n';
import { folderMarkKind, folderMarks, markCounts, markKey, type FileMark, type FileMarks, type FolderMark } from './file-activity';
import './file-tree.css';

/**
 * #247 — the workspace file tree in the side panel, lit up with what the
 * current turn has read (eye) and written (pencil). The marks come from
 * `file-activity.ts`; this component only lists folders lazily
 * (`/api/workspace/browse?include_files=true`) and paints them.
 */

type Listing = BrowseResult | 'loading' | { error: string };

const markLabel = (mark: FileMark) => t(mark === 'written' ? 'Written in this turn' : 'Read in this turn');
const underLabel = (mark: FileMark) => t(mark === 'written' ? 'Holds files written in this turn' : 'Holds files read in this turn');

function MarkIcon({ mark, size = 12 }: { mark: FileMark; size?: number }) {
  const Icon = mark === 'written' ? Pencil : Eye;
  return <Icon size={size} aria-hidden="true" />;
}

export function FileTreeLegend({ marks }: { marks: FileMarks }) {
  const { read, written } = markCounts(marks);
  return (
    <div className="fs-ftree__legend" role="group" aria-label={t('Files touched in this turn')} data-testid="file-tree-legend">
      <span className="fs-ftree__chip" data-mark="written" data-testid="file-tree-count-written">
        <MarkIcon mark="written" />{t('{n} written', { n: written })}
      </span>
      <span className="fs-ftree__chip" data-mark="read" data-testid="file-tree-count-read">
        <MarkIcon mark="read" />{t('{n} read', { n: read })}
      </span>
    </div>
  );
}

interface TreeProps {
  /** The folder the tree is rooted at, as the server spells it (keys are relative to it). */
  base: string;
  marks: FileMarks;
  folders: Record<string, FolderMark>;
  listings: Record<string, Listing>;
  expanded: Record<string, boolean>;
  toggle: (path: string) => void;
  open: (path: string) => void;
  active: string | null;
}

function Branch({ dir, depth, props }: { dir: string; depth: number; props: TreeProps }) {
  const { base, marks, folders, listings, expanded, toggle, open, active } = props;
  const listing = listings[dir];
  if (!listing || listing === 'loading') return <li className="fs-ftree__note" role="none" style={{ paddingLeft: 8 + depth * 14 }}>{t('Loading…')}</li>;
  if ('error' in listing) return <li className="fs-ftree__note" role="none" style={{ paddingLeft: 8 + depth * 14 }}>{t('Could not list this folder')}</li>;
  const rows = [
    ...listing.dirs.map((d) => ({ kind: 'dir' as const, name: d.name, path: d.path })),
    ...listing.files.map((f) => ({ kind: 'file' as const, name: f.name, path: f.path })),
  ];
  return (
    <>
      {rows.map((row) => {
        const key = markKey(row.path, base) ?? '';
        if (row.kind === 'file') {
          const mark = marks[key];
          return (
            <li key={row.path} role="treeitem" aria-selected={active === row.path || undefined} className="fs-ftree__item">
              <button
                type="button"
                className="fs-ftree__row"
                data-testid="file-tree-file"
                data-path={key}
                data-mark={mark}
                data-active={active === row.path || undefined}
                style={{ paddingLeft: 8 + depth * 14 }}
                title={row.path}
                aria-label={mark ? `${row.name} · ${markLabel(mark)}` : undefined}
                onClick={() => open(row.path)}
              >
                <FileIcon size={13} aria-hidden="true" className="fs-ftree__icon" />
                <span className="fs-ftree__name">{row.name}</span>
                {mark && <span className="fs-ftree__mark" data-mark={mark}><MarkIcon mark={mark} /></span>}
              </button>
            </li>
          );
        }
        const isOpen = Boolean(expanded[row.path]);
        const under = folderMarkKind(folders[key]);
        return (
          <li key={row.path} role="treeitem" aria-expanded={isOpen} className="fs-ftree__item">
            <button
              type="button"
              className="fs-ftree__row"
              data-testid="file-tree-folder"
              data-path={key}
              data-under={under ?? undefined}
              style={{ paddingLeft: 8 + depth * 14 }}
              title={row.path}
              aria-label={under ? `${row.name} · ${underLabel(under)}` : undefined}
              onClick={() => toggle(row.path)}
            >
              {isOpen ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
              {isOpen ? <FolderOpen size={13} aria-hidden="true" className="fs-ftree__icon" /> : <Folder size={13} aria-hidden="true" className="fs-ftree__icon" />}
              <span className="fs-ftree__name">{row.name}</span>
              {under && <span className="fs-ftree__under" data-mark={under} aria-hidden="true" />}
            </button>
            {isOpen && <ul role="group" className="fs-ftree__list"><Branch dir={row.path} depth={depth + 1} props={props} /></ul>}
          </li>
        );
      })}
      {!rows.length && <li className="fs-ftree__note" role="none" style={{ paddingLeft: 8 + depth * 14 }}>{t('Empty folder')}</li>}
      {listing.truncated && <li className="fs-ftree__note" role="none" style={{ paddingLeft: 8 + depth * 14 }}>{t('Showing the first entries only')}</li>}
    </>
  );
}

export interface FileTreeProps {
  workspace: string;
  marks: FileMarks;
  /** Changes whenever the agent may have created or deleted files (written count, turn end). */
  refresh: string;
  onOpen: (path: string) => void;
  active: string | null;
}

export default function FileTree({ workspace, marks, refresh, onOpen, active }: FileTreeProps) {
  const [listings, setListings] = useState<Record<string, Listing>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const aborts = useRef(new Map<string, AbortController>());
  const folders = useMemo(() => folderMarks(marks), [marks]);

  const load = useCallback((dir: string, quiet: boolean) => {
    aborts.current.get(dir)?.abort();
    const controller = new AbortController();
    aborts.current.set(dir, controller);
    if (!quiet) setListings((cur) => ({ ...cur, [dir]: 'loading' }));
    browseWorkspace(dir, controller.signal, true)
      .then((result) => { if (!controller.signal.aborted) setListings((cur) => ({ ...cur, [dir]: result })); })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        // A quiet refresh that fails keeps the listing it already has.
        if (!quiet) setListings((cur) => ({ ...cur, [dir]: { error: error instanceof Error ? error.message : String(error) } }));
      });
  }, []);

  // A different workspace is a different tree.
  useEffect(() => {
    const pending = aborts.current;
    setListings({});
    setExpanded({});
    load(workspace, false);
    return () => { pending.forEach((c) => c.abort()); pending.clear(); };
  }, [workspace, load]);

  // Files the agent created or removed: re-list the root and every open folder, without flashing.
  const seen = useRef(refresh);
  useEffect(() => {
    if (seen.current === refresh) return;
    seen.current = refresh;
    load(workspace, true);
    for (const [dir, isOpen] of Object.entries(expanded)) if (isOpen) load(dir, true);
  }, [refresh, workspace, expanded, load]);

  const toggle = useCallback((path: string) => {
    setExpanded((cur) => {
      const next = !cur[path];
      if (next) load(path, false);
      return { ...cur, [path]: next };
    });
  }, [load]);

  const rootListing = listings[workspace];
  const base = rootListing && typeof rootListing === 'object' && 'path' in rootListing ? rootListing.path : workspace;
  const props: TreeProps = { base, marks, folders, listings, expanded, toggle, open: onOpen, active };
  return (
    <div className="fs-panel__body fs-ftree" data-testid="file-tree" data-marks={JSON.stringify(marks)}>
      <div className="fs-ftree__head">
        <h3 title={workspace}>{basename(workspace) || workspace}</h3>
        <FileTreeLegend marks={marks} />
      </div>
      <p className="fs-ftree__hint">{t('Files the agent reads or writes in the current turn light up here until the next message.')}</p>
      <ul role="tree" className="fs-ftree__list fs-ftree__root" aria-label={t('Workspace files')}>
        <Branch dir={workspace} depth={0} props={props} />
      </ul>
    </div>
  );
}
