import { Files, Network, Info } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { createPortal } from 'react-dom';
import { ensureOverlayRoot } from '../shell/overlayRoot';
import { Button, Dialog } from '../components';
import {
  createNote,
  deleteNote,
  loadDaily,
  loadEntityProfile,
  loadNote,
  loadStatus,
  loadTags,
  loadTree,
  loadTrash,
  renameNote,
  runSync,
  searchNotes,
  todayIso,
  type BrainStatus,
  type NoteTree,
  type ReadNote,
  type SearchHit,
  type TagCount,
} from '../adapters/brain';
import { buildTitleIndex } from '../lib/wikilinks';
import { usePlatform } from '../shell/platform';
import { Explorer } from './brain/Explorer';
import { NoteView, type NoteMode } from './brain/NoteView';
import { RightPanel } from './brain/RightPanel';
import { GraphView } from './brain/GraphView';
import { QuickSwitcher } from './brain/QuickSwitcher';
import { SettingsDrawer } from './brain/SettingsDrawer';
import { TrashPanel } from './brain/TrashPanel';
import { SyncBar } from './brain/SyncBar';
import { t } from '../i18n';
import './projects.css';
import './brain.css';

/**
 * The Brain screen: a markdown-vault note app inside Faustus, built against
 * the backend route contract in the shared build contract — Lot C ships the
 * `/api/brain` routes to that exact shape; this screen never guesses at a
 * different one. Every reader in `adapters/brain.ts` already tolerates a
 * missing field, so the screen itself only has to handle three states per
 * panel: loading, error and empty, same as everywhere else in Studio.
 */
export function BrainScreen() {
  const platform = usePlatform();
  const [params, setParams] = useSearchParams();
  const activePath = params.get('note') ?? '';
  const view = params.get('view') === 'graph' ? 'graph' : 'note';
  const [mobilePane, setMobilePane] = useState<'files' | 'note' | 'graph' | 'info'>('files');

  const [status, setStatus] = useState<BrainStatus | null>(null);
  const [tree, setTree] = useState<NoteTree | null>(null);
  const [treeLoading, setTreeLoading] = useState(true);
  const [tags, setTags] = useState<TagCount[]>([]);
  const [trashCount, setTrashCount] = useState(0);
  const [syncing, setSyncing] = useState(false);

  const [note, setNote] = useState<ReadNote | null>(null);
  const [noteLoading, setNoteLoading] = useState(false);
  const [noteError, setNoteError] = useState<unknown>(null);
  const [mode, setMode] = useState<NoteMode>('read');
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;

  const [query, setQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [trashOpen, setTrashOpen] = useState(false);
  const [pendingNav, setPendingNav] = useState<(() => void) | null>(null);
  const [saveToken, setSaveToken] = useState(0);
  const [focusToken, setFocusToken] = useState(0);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const autoOpened = useRef(false);

  const reloadStatus = useCallback(() => void loadStatus().then(setStatus).catch(() => undefined), []);
  const reloadTree = useCallback(() => {
    setTreeLoading(true);
    return loadTree()
      .then((t) => setTree(t))
      .catch(() => undefined)
      .finally(() => setTreeLoading(false));
  }, []);
  const reloadTags = useCallback(() => void loadTags().then(setTags).catch(() => undefined), []);
  const reloadTrashCount = useCallback(() => void loadTrash().then((items) => setTrashCount(items.length)).catch(() => undefined), []);

  useEffect(() => {
    reloadStatus();
    void reloadTree();
    reloadTags();
    reloadTrashCount();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Home.md, when there is one, is the vault's own front page — open it the
  // first time the screen has a tree and nothing is already selected.
  useEffect(() => {
    if (autoOpened.current || activePath || !tree) return;
    autoOpened.current = true;
    const home = tree.notes.find((n) => n.path === 'Home.md');
    if (home) setParams((p) => { const next = new URLSearchParams(p); next.set('note', home.path); return next; }, { replace: true });
  }, [tree, activePath, setParams]);

  useEffect(() => {
    if (!activePath) {
      setNote(null);
      return;
    }
    const controller = new AbortController();
    setNoteLoading(true);
    setNoteError(null);
    setMode('read');
    loadNote(activePath, controller.signal)
      .then(setNote)
      .catch((e) => {
        if (!controller.signal.aborted) setNoteError(e);
      })
      .finally(() => {
        if (!controller.signal.aborted) setNoteLoading(false);
      });
    return () => controller.abort();
  }, [activePath]);

  useEffect(() => {
    if (!query.trim()) {
      setSearchResults(null);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    const timer = window.setTimeout(() => {
      searchNotes(query, 40, controller.signal)
        .then(setSearchResults)
        .catch(() => setSearchResults([]))
        .finally(() => setSearching(false));
    }, 220);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query]);

  const titleIndex = useMemo(() => buildTitleIndex(tree?.notes ?? []), [tree]);

  /** Any navigation that would drop the current draft goes through here:
   *  unsaved edits ask first, exactly once, regardless of where the click
   *  that triggered the navigation came from (explorer, switcher, a
   *  wikilink, a backlink…). */
  const guardedNavigate = useCallback(
    (action: () => void) => {
      if (dirtyRef.current) setPendingNav(() => action);
      else action();
    },
    [],
  );

  const openNote = useCallback(
    (path: string, nextMode: NoteMode = 'read') => {
      guardedNavigate(() => {
        setParams((p) => { const next = new URLSearchParams(p); next.set('note', path); next.delete('view'); return next; });
        setMode(nextMode);
        if (platform === 'mobile') setMobilePane('note');
      });
    },
    [guardedNavigate, setParams, platform],
  );

  const openDaily = useCallback(() => {
    guardedNavigate(async () => {
      const d = await loadDaily(todayIso());
      await reloadTree();
      setParams((p) => { const next = new URLSearchParams(p); next.set('note', d.path); next.delete('view'); return next; });
      setMode('read');
    });
  }, [guardedNavigate, reloadTree, setParams]);

  const createAndOpen = useCallback(
    async (title: string, folder = 'Notes') => {
      const created = await createNote(title, folder);
      await reloadTree();
      setParams((p) => { const next = new URLSearchParams(p); next.set('note', created.path); next.delete('view'); return next; });
      setMode('edit');
    },
    [reloadTree, setParams],
  );

  const [newNoteOpen, setNewNoteOpen] = useState(false);
  const [newNoteTitle, setNewNoteTitle] = useState('');

  function onEntityOpen(id: string) {
    void loadEntityProfile(id).then((profile) => {
      if (profile.path) openNote(profile.path);
    });
  }

  return (
    <div className="fs-brain" data-testid="brain-screen" data-platform-pane={platform === 'mobile' ? mobilePane : undefined}>
      <SyncBar
        status={status}
        syncing={syncing}
        onSync={() => {
          setSyncing(true);
          void runSync()
            .then(() => Promise.all([reloadStatus(), reloadTree(), reloadTags(), reloadTrashCount()]))
            .finally(() => setSyncing(false));
        }}
      />

      {platform === 'mobile' && (
        <div className="fs-seg fs-brain__mobile-tabs" role="radiogroup" aria-label={t('View')}>
          <button type="button" role="radio" aria-checked={mobilePane === 'files'} onClick={() => setMobilePane('files')}>
            <Files size={13} aria-hidden="true" /> {t('Files')}
          </button>
          <button type="button" role="radio" aria-checked={mobilePane === 'note'} onClick={() => setMobilePane('note')}>
            {t('Note')}
          </button>
          <button type="button" role="radio" aria-checked={mobilePane === 'graph'} onClick={() => setMobilePane('graph')}>
            <Network size={13} aria-hidden="true" /> {t('Graph')}
          </button>
          <button type="button" role="radio" aria-checked={mobilePane === 'info'} onClick={() => setMobilePane('info')} disabled={!note}>
            <Info size={13} aria-hidden="true" /> {t('Details')}
          </button>
        </div>
      )}

      <div className="fs-brain__body">
        {(platform !== 'mobile' || mobilePane === 'files') && (
          <Explorer
            tree={tree}
            loading={treeLoading}
            activePath={activePath}
            onOpen={openNote}
            onNew={() => { setNewNoteTitle(''); setNewNoteOpen(true); }}
            query={query}
            onQuery={setQuery}
            searchResults={searchResults}
            searching={searching}
            tags={tags}
            activeTag={query}
            onTag={(tg) => setQuery(tg)}
            trashCount={trashCount}
            onOpenTrash={() => setTrashOpen(true)}
            onOpenGraph={() => (platform === 'mobile' ? setMobilePane('graph') : setParams((p) => { const next = new URLSearchParams(p); next.set('view', 'graph'); return next; }))}
            onOpenSettings={() => setSettingsOpen(true)}
            searchInputRef={searchInputRef}
          />
        )}

        {(platform !== 'mobile' || mobilePane === 'note' || mobilePane === 'graph') && (
          <div className="fs-brain__centre">
            {platform !== 'mobile' && (
              <div className="fs-seg fs-brain__view-tabs" role="radiogroup" aria-label={t('View')}>
                <button type="button" role="radio" aria-checked={view === 'note'} onClick={() => setParams((p) => { const next = new URLSearchParams(p); next.delete('view'); return next; })}>
                  {t('Note')}
                </button>
                <button type="button" role="radio" aria-checked={view === 'graph'} onClick={() => setParams((p) => { const next = new URLSearchParams(p); next.set('view', 'graph'); return next; })} data-testid="brain-view-graph">
                  <Network size={13} aria-hidden="true" /> {t('Graph')}
                </button>
              </div>
            )}
            {(platform === 'mobile' ? mobilePane === 'graph' : view === 'graph') ? (
              <GraphView center={activePath || undefined} onOpenNote={openNote} onOpenEntity={onEntityOpen} />
            ) : (
              <NoteView
                note={note}
                loading={noteLoading}
                error={noteError}
                mode={mode}
                onModeChange={setMode}
                titleIndex={titleIndex}
                allNotes={tree?.notes ?? []}
                onOpenNote={openNote}
                onCreateNote={(title) => void createAndOpen(title)}
                onSaved={(saved) => {
                  setNote(saved);
                  // A memory note whose text changed comes back at a NEW
                  // path (its id is derived from the text) — follow it, so
                  // the address bar and the tree selection never point at a
                  // file that no longer exists.
                  if (saved.path !== activePath) {
                    setParams((p) => { const next = new URLSearchParams(p); next.set('note', saved.path); return next; }, { replace: true });
                  }
                  void reloadTree();
                  reloadTags();
                }}
                onDirtyChange={setDirty}
                onDeleted={() => {
                  if (!activePath) return;
                  void deleteNote(activePath).then(() => {
                    setParams((p) => { const next = new URLSearchParams(p); next.delete('note'); return next; });
                    void reloadTree();
                    reloadTrashCount();
                  });
                }}
                onRenamed={(newTitle) => {
                  if (!activePath) return;
                  void renameNote(activePath, newTitle).then((outcome) => {
                    setNote(outcome.note);
                    void reloadTree();
                    setParams((p) => { const next = new URLSearchParams(p); next.set('note', outcome.note.path); return next; });
                  });
                }}
                saveToken={saveToken}
                focusToken={focusToken}
              />
            )}
          </div>
        )}

        {note && (platform !== 'mobile' || mobilePane === 'info') && (
          <RightPanel note={note} onOpenNote={openNote} onEntityChanged={() => setNote((n) => (n ? { ...n } : n))} />
        )}
      </div>

      {/* Overlays go to the shared overlay root: every screen's children
          carry the entrance transform, which would make `position: fixed`
          relative to the screen and push the drawers half off-screen. */}
      {createPortal(
        <>
        {switcherOpen && (
          <QuickSwitcher
            notes={tree?.notes ?? []}
            onPick={(path) => openNote(path)}
            onDaily={() => openDaily()}
            onClose={() => setSwitcherOpen(false)}
          />
        )}
        {settingsOpen && <SettingsDrawer onClose={() => setSettingsOpen(false)} />}
        {trashOpen && (
          <TrashPanel
            onClose={() => setTrashOpen(false)}
            onRestored={(path) => {
              void reloadTree();
              reloadTrashCount();
              openNote(path);
            }}
          />
        )}
        </>,
        ensureOverlayRoot(),
      )}

      <Dialog
        open={newNoteOpen}
        onOpenChange={setNewNoteOpen}
        title={t('New note')}
        testId="brain-new-note-dialog"
        footer={
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setNewNoteOpen(false)} />
            <Button
              variant="primary"
              size="sm"
              label={t('Create')}
              disabled={!newNoteTitle.trim()}
              onClick={() => {
                const title = newNoteTitle.trim();
                setNewNoteOpen(false);
                void createAndOpen(title);
              }}
              testId="brain-new-note-confirm"
            />
          </>
        }
      >
        <input
          className="fs-field"
          autoFocus
          value={newNoteTitle}
          onChange={(e) => setNewNoteTitle(e.target.value)}
          placeholder={t('Title')}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && newNoteTitle.trim()) {
              setNewNoteOpen(false);
              void createAndOpen(newNoteTitle.trim());
            }
          }}
        />
      </Dialog>

      <Dialog
        open={pendingNav !== null}
        onOpenChange={(open) => { if (!open) setPendingNav(null); }}
        title={t('Discard unsaved changes?')}
        description={t('This note has edits that have not been saved.')}
        testId="brain-discard-dialog"
        footer={
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setPendingNav(null)} />
            <Button
              variant="danger-solid"
              size="sm"
              label={t('Discard and continue')}
              onClick={() => {
                const action = pendingNav;
                setPendingNav(null);
                setDirty(false);
                dirtyRef.current = false;
                action?.();
              }}
              testId="brain-discard-confirm"
            />
          </>
        }
      />

      <KeyboardShortcuts
        hasNote={Boolean(note)}
        editable={Boolean(note?.editable)}
        onQuickSwitcher={() => setSwitcherOpen(true)}
        onToggleMode={() => setMode((m) => (m === 'edit' ? 'read' : 'edit'))}
        onSave={() => setSaveToken((n) => n + 1)}
        onFocusSearch={() => {
          setFocusToken((n) => n + 1);
          if (platform === 'mobile') setMobilePane('files');
          requestAnimationFrame(() => searchInputRef.current?.focus());
        }}
        onEscape={() => {
          if (switcherOpen) setSwitcherOpen(false);
          else if (settingsOpen) setSettingsOpen(false);
          else if (trashOpen) setTrashOpen(false);
        }}
      />
    </div>
  );
}

/** One place for every shortcut this screen owns, so `Brain.tsx`'s render
 *  body stays about layout — Ctrl+O quick switcher, Ctrl+E read/edit,
 *  Ctrl+S save, Ctrl+Shift+F focus search, Escape closes whatever overlay
 *  is open (checked in that order by the caller). */
function KeyboardShortcuts({
  hasNote,
  editable,
  onQuickSwitcher,
  onToggleMode,
  onSave,
  onFocusSearch,
  onEscape,
}: {
  hasNote: boolean;
  editable: boolean;
  onQuickSwitcher: () => void;
  onToggleMode: () => void;
  onSave: () => void;
  onFocusSearch: () => void;
  onEscape: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.ctrlKey || e.metaKey;
      if (!mod && e.key === 'Escape') {
        onEscape();
        return;
      }
      if (!mod) return;
      const key = e.key.toLowerCase();
      if (key === 'o') {
        e.preventDefault();
        onQuickSwitcher();
      } else if (key === 'e' && hasNote && editable) {
        e.preventDefault();
        onToggleMode();
      } else if (key === 's') {
        e.preventDefault();
        onSave();
      } else if (e.shiftKey && key === 'f') {
        e.preventDefault();
        onFocusSearch();
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [hasNote, editable, onQuickSwitcher, onToggleMode, onSave, onFocusSearch, onEscape]);
  return null;
}
