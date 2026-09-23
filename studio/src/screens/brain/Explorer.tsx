import { Calendar, ChevronDown, ChevronRight, FileText, Folder, FolderOpen, Layers, Network, Plus, Search, Settings2, Sparkles, Tag, Trash2, User } from 'lucide-react';
import { useMemo, useState, type RefObject } from 'react';
import { IconButton } from '../../components';
import type { NoteSummary, NoteTree, SearchHit, TagCount } from '../../adapters/brain';
import { t } from '../../i18n';

/**
 * The left pane: the vault's file tree, the search box, the tag list and
 * the trash/graph/settings entry points. The tree is rebuilt from the flat
 * `notes.tree()` answer on every render of a changed tree — cheap at vault
 * sizes this screen targets, and it means a folder collapsed by a person
 * stays exactly where they left it (kept in this component's own state,
 * not derived from the server).
 */

const KIND_ICON: Record<string, typeof FileText> = {
  entity: User,
  daily: Calendar,
  home: Sparkles,
  project: Layers,
  objective: Layers,
  concept: Network,
};

function kindIcon(kind: string) {
  return KIND_ICON[kind] ?? FileText;
}

interface FolderNode {
  name: string;
  path: string;
  children: Map<string, FolderNode>;
  notes: NoteSummary[];
}

function newFolder(name: string, path: string): FolderNode {
  return { name, path, children: new Map(), notes: [] };
}

function buildTree(tree: NoteTree): FolderNode {
  const root = newFolder('', '');
  const ensure = (path: string): FolderNode => {
    if (!path) return root;
    const parts = path.split('/');
    let node = root;
    let acc = '';
    for (const part of parts) {
      acc = acc ? `${acc}/${part}` : part;
      let next = node.children.get(part);
      if (!next) {
        next = newFolder(part, acc);
        node.children.set(part, next);
      }
      node = next;
    }
    return node;
  };
  for (const folder of tree.folders) ensure(folder);
  for (const note of tree.notes) {
    const slash = note.path.lastIndexOf('/');
    const folder = slash >= 0 ? note.path.slice(0, slash) : '';
    ensure(folder).notes.push(note);
  }
  return root;
}

function FolderRow({ node, depth, activePath, open, onToggle, onOpen }: { node: FolderNode; depth: number; activePath: string; open: Set<string>; onToggle: (path: string) => void; onOpen: (path: string) => void }) {
  const isOpen = depth === 0 || open.has(node.path);
  const children = [...node.children.values()].sort((a, b) => a.name.localeCompare(b.name));
  const notes = node.notes.slice().sort((a, b) => a.title.localeCompare(b.title));
  return (
    <>
      {depth > 0 && (
        <button type="button" className="fs-brain__folder" style={{ paddingInlineStart: `${8 + depth * 14}px` }} onClick={() => onToggle(node.path)} aria-expanded={isOpen} data-testid="brain-folder">
          {isOpen ? <ChevronDown size={13} aria-hidden="true" /> : <ChevronRight size={13} aria-hidden="true" />}
          {isOpen ? <FolderOpen size={14} aria-hidden="true" /> : <Folder size={14} aria-hidden="true" />}
          {node.name}
        </button>
      )}
      {isOpen && (
        <>
          {notes.map((note) => {
            const Icon = kindIcon(note.kind);
            return (
              <button
                key={note.path}
                type="button"
                className="fs-brain__note-row"
                style={{ paddingInlineStart: `${8 + (depth + 1) * 14}px` }}
                data-active={note.path === activePath || undefined}
                onClick={() => onOpen(note.path)}
                data-testid="brain-note-row"
                title={note.path}
              >
                <Icon size={13} aria-hidden="true" data-kind={note.kind} />
                <span className="fs-brain__note-title">{note.title}</span>
              </button>
            );
          })}
          {children.map((child) => (
            <FolderRow key={child.path} node={child} depth={depth + 1} activePath={activePath} open={open} onToggle={onToggle} onOpen={onOpen} />
          ))}
        </>
      )}
    </>
  );
}

export function Explorer({
  tree,
  loading,
  activePath,
  onOpen,
  onNew,
  query,
  onQuery,
  searchResults,
  searching,
  tags,
  activeTag,
  onTag,
  trashCount,
  onOpenTrash,
  onOpenGraph,
  onOpenSettings,
  searchInputRef,
}: {
  tree: NoteTree | null;
  loading: boolean;
  activePath: string;
  onOpen: (path: string) => void;
  onNew: () => void;
  query: string;
  onQuery: (q: string) => void;
  searchResults: SearchHit[] | null;
  searching: boolean;
  tags: TagCount[];
  activeTag: string;
  onTag: (tag: string) => void;
  trashCount: number;
  onOpenTrash: () => void;
  onOpenGraph: () => void;
  onOpenSettings: () => void;
  searchInputRef?: RefObject<HTMLInputElement | null>;
}) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [tagsOpen, setTagsOpen] = useState(false);
  const toggle = (path: string) => setOpen((s) => { const next = new Set(s); if (next.has(path)) next.delete(path); else next.add(path); return next; });

  const root = useMemo(() => (tree ? buildTree(tree) : null), [tree]);

  return (
    <aside className="fs-brain__explorer" aria-label={t('Notes')}>
      <div className="fs-brain__explorer-head">
        <label className="fs-search fs-brain__search">
          <Search size={13} aria-hidden="true" />
          <input
            ref={searchInputRef}
            type="search"
            value={query}
            placeholder={t('Search the vault…')}
            aria-label={t('Search the vault')}
            onChange={(e) => onQuery(e.target.value)}
            data-testid="brain-search"
          />
        </label>
        <IconButton icon={Plus} label={t('New note')} size="sm" onClick={onNew} testId="brain-new-note" />
      </div>

      <div className="fs-brain__explorer-tools">
        <IconButton icon={Network} label={t('Graph view')} size="sm" onClick={onOpenGraph} testId="brain-open-graph" />
        <IconButton icon={Trash2} label={t('Trash')} size="sm" badge={trashCount} onClick={onOpenTrash} testId="brain-open-trash" />
        <IconButton icon={Settings2} label={t('Vault settings')} size="sm" onClick={onOpenSettings} testId="brain-open-settings" />
      </div>

      {query.trim() ? (
        <div className="fs-brain__search-results" data-testid="brain-search-results">
          {searching && <p className="fs-muted">{t('Searching…')}</p>}
          {!searching && searchResults && searchResults.length === 0 && <p className="fs-muted">{t('No note matches "{query}".', { query })}</p>}
          {!searching && searchResults?.map((hit) => {
            const Icon = kindIcon(hit.kind);
            return (
              <button key={hit.path} type="button" className="fs-brain__note-row" data-active={hit.path === activePath || undefined} onClick={() => onOpen(hit.path)}>
                <Icon size={13} aria-hidden="true" />
                <span className="fs-brain__note-title">{hit.title}</span>
                {hit.snippet && <span className="fs-brain__snippet">{hit.snippet}</span>}
              </button>
            );
          })}
        </div>
      ) : (
        <div className="fs-brain__tree" data-testid="brain-tree">
          {loading && <p className="fs-muted">{t('Reading the vault…')}</p>}
          {!loading && root && root.notes.length === 0 && root.children.size === 0 && (
            <p className="fs-muted fs-brain__tree-empty">{t('The vault is empty — create the first note.')}</p>
          )}
          {!loading && root && <FolderRow node={root} depth={0} activePath={activePath} open={open} onToggle={toggle} onOpen={onOpen} />}
        </div>
      )}

      <div className="fs-brain__tags">
        <button type="button" className="fs-brain__tags-head" onClick={() => setTagsOpen((v) => !v)} aria-expanded={tagsOpen}>
          {tagsOpen ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
          <Tag size={12} aria-hidden="true" /> {t('Tags')} <span className="fs-muted">({tags.length})</span>
        </button>
        {tagsOpen && (
          <div className="fs-brain__tag-list">
            {tags.length === 0 && <p className="fs-muted">{t('No tags yet.')}</p>}
            {tags.map((tg) => (
              <button key={tg.tag} type="button" className="fs-brain__tag-chip" data-active={tg.tag === activeTag || undefined} onClick={() => onTag(tg.tag === activeTag ? '' : tg.tag)}>
                #{tg.tag} <b>{tg.count}</b>
              </button>
            ))}
          </div>
        )}
      </div>
    </aside>
  );
}
