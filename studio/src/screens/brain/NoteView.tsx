import { ChevronRight, Eye, FilePenLine, Save, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button, Dialog, EmptyState, IconButton, Skeleton } from '../../components';
import { composeNoteContent, writeNote, type NoteSummary, type ReadNote } from '../../adapters/brain';
import { activeWikiAutocomplete, applyWikiAutocomplete, fuzzySearch, type TitleIndex } from '../../lib/wikilinks';
import { NoteMarkdown } from './NoteMarkdown';
import { Properties } from './Properties';
import { locale, t } from '../../i18n';

export type NoteMode = 'read' | 'edit';

/**
 * The centre pane: reading view (the shared renderer, wikilinks live) and
 * editing view (a plain textarea over the user zone only — the generated
 * zone renders read-only underneath, exactly as the vault file has it, so
 * nobody ever hand-edits a section the next sync would overwrite anyway).
 *
 * All the state that a keyboard shortcut needs to reach from `Brain.tsx`
 * (save, the dirty flag) is either lifted there or driven by a counter prop
 * (`saveToken`) rather than a ref — `Brain.tsx` owns the *fact* of "there
 * are unsaved changes" so it can guard a note switch; this component owns
 * the draft text itself.
 */
export function NoteView({
  note,
  loading,
  error,
  mode,
  onModeChange,
  titleIndex,
  allNotes,
  onOpenNote,
  onCreateNote,
  onSaved,
  onDirtyChange,
  onDeleted,
  onRenamed,
  saveToken,
  focusToken,
}: {
  note: ReadNote | null;
  loading: boolean;
  error: unknown;
  mode: NoteMode;
  onModeChange: (m: NoteMode) => void;
  titleIndex: TitleIndex;
  allNotes: NoteSummary[];
  onOpenNote: (path: string) => void;
  onCreateNote: (title: string) => void;
  onSaved: (note: ReadNote) => void;
  onDirtyChange: (dirty: boolean) => void;
  onDeleted: () => void;
  onRenamed: (newTitle: string) => void;
  saveToken: number;
  focusToken: number;
}) {
  const [draft, setDraft] = useState('');
  const [frontmatterPatch, setFrontmatterPatch] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [autocomplete, setAutocomplete] = useState<{ start: number; end: number; query: string } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    setDraft(note?.userZone ?? '');
    setFrontmatterPatch({});
    setSaveError(null);
    setAutocomplete(null);
  }, [note?.path]);

  const frontmatter = useMemo(() => ({ ...(note?.frontmatter ?? {}), ...frontmatterPatch }), [note?.frontmatter, frontmatterPatch]);
  const dirty = Boolean(note) && mode === 'edit' && (draft !== note!.userZone || Object.keys(frontmatterPatch).length > 0);

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  async function save() {
    if (!note || !dirty || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      // only the keys the person changed are rewritten in the file's YAML
      const content = composeNoteContent(note, draft, frontmatterPatch);
      const outcome = await writeNote(note.path, content);
      setFrontmatterPatch({});
      onSaved(outcome.note);
    } catch (e) {
      setSaveError(e);
    } finally {
      setSaving(false);
    }
  }

  // Ctrl+S from Brain.tsx's global handler bumps this counter.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (saveToken > 0) void save();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saveToken]);

  useEffect(() => {
    if (focusToken > 0 && mode === 'edit') textareaRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusToken]);

  const autocompleteHits = autocomplete ? fuzzySearch(autocomplete.query, allNotes, (n) => n.title, 8) : [];

  function onTextareaChange(value: string) {
    setDraft(value);
    const caret = textareaRef.current?.selectionStart ?? value.length;
    setAutocomplete(activeWikiAutocomplete(value, caret));
  }

  function pickAutocomplete(title: string) {
    if (!autocomplete) return;
    const { text, caret } = applyWikiAutocomplete(draft, autocomplete, title);
    setDraft(text);
    setAutocomplete(null);
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(caret, caret);
      }
    });
  }

  if (loading) return <Skeleton label={t('Reading the note')} count={6} height="20px" />;
  if (!note) {
    if (error) return <EmptyState tone="error" title={t('The note could not be read')} body={String((error as Error)?.message ?? error)} />;
    return <EmptyState title={t('No note open')} body={t('Pick a note from the list, or press Ctrl+O to jump to one.')} />;
  }

  const crumbs = note.path.split('/').filter(Boolean);

  return (
    <section className="fs-brain__note" data-testid="brain-note-view" data-kind={note.kind}>
      <header className="fs-brain__note-head">
        <nav className="fs-brain__breadcrumbs" aria-label={t('Breadcrumbs')}>
          {crumbs.map((seg, i) => (
            <span key={i}>
              {i > 0 && <ChevronRight size={11} aria-hidden="true" />}
              {i === crumbs.length - 1 ? seg.replace(/\.md$/, '') : seg}
            </span>
          ))}
        </nav>
        <div className="fs-brain__note-title-row">
          {renaming ? (
            <input
              className="fs-brain__title-input"
              value={renameValue}
              autoFocus
              onChange={(e) => setRenameValue(e.target.value)}
              onBlur={() => {
                setRenaming(false);
                if (renameValue.trim() && renameValue.trim() !== note.title) onRenamed(renameValue.trim());
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                if (e.key === 'Escape') setRenaming(false);
              }}
            />
          ) : (
            <h2
              className="fs-brain__note-title"
              onDoubleClick={() => {
                if (note.kind === 'note' || note.kind === 'entity') {
                  setRenameValue(note.title);
                  setRenaming(true);
                }
              }}
            >
              {note.title}
            </h2>
          )}
          <span className="fs-muted fs-brain__note-updated">{note.updatedAt ? new Date(note.updatedAt).toLocaleString(locale()) : ''}</span>
          <span className="fs-spacer" />
          <div className="fs-seg" role="radiogroup" aria-label={t('View')}>
            <button type="button" role="radio" aria-checked={mode === 'read'} onClick={() => onModeChange('read')} data-testid="brain-mode-read">
              <Eye size={13} aria-hidden="true" /> {t('Read')}
            </button>
            <button type="button" role="radio" aria-checked={mode === 'edit'} disabled={!note.editable} onClick={() => onModeChange('edit')} data-testid="brain-mode-edit">
              <FilePenLine size={13} aria-hidden="true" /> {t('Edit')}
            </button>
          </div>
          {mode === 'edit' && (
            <Button variant="primary" size="sm" icon={Save} label={saving ? t('Saving…') : t('Save')} disabled={!dirty} loading={saving} onClick={() => void save()} testId="brain-save" />
          )}
          <IconButton icon={Trash2} label={t('Delete this note')} size="sm" onClick={() => setConfirmDelete(true)} testId="brain-delete" />
        </div>
        {!note.editable && <p className="fs-notice" data-tone="warning">{t('This note is read-mostly: only its free annotation is editable, if any.')}</p>}
        {saveError != null && <p className="fs-notice" data-tone="danger">{String((saveError as Error)?.message ?? saveError)}</p>}
      </header>

      <Properties note={{ ...note, frontmatter }} onChange={(patch) => setFrontmatterPatch((cur) => ({ ...cur, ...patch }))} />

      {mode === 'read' ? (
        <div className="fs-brain__reading">
          <NoteMarkdown body={note.userZone} index={titleIndex} onOpenNote={onOpenNote} onCreateNote={onCreateNote} />
          {note.generated.trim() && (
            <div className="fs-brain__generated" data-testid="brain-generated-zone">
              <p className="fs-brain__generated-label">{t('Generated — replaced on the next sync')}</p>
              <NoteMarkdown body={note.generated} index={titleIndex} onOpenNote={onOpenNote} onCreateNote={onCreateNote} />
            </div>
          )}
        </div>
      ) : (
        <div className="fs-brain__editing">
          <div className="fs-brain__editor-wrap">
            <textarea
              ref={textareaRef}
              className="fs-brain__editor"
              value={draft}
              onChange={(e) => onTextareaChange(e.target.value)}
              onBlur={() => void save()}
              onKeyDown={(e) => {
                if (autocomplete && (e.key === 'Escape')) {
                  e.stopPropagation();
                  setAutocomplete(null);
                }
              }}
              spellCheck={false}
              data-testid="brain-editor"
              aria-label={t('Note body')}
            />
            {autocomplete && autocompleteHits.length > 0 && (
              <ul className="fs-brain__autocomplete" data-testid="brain-autocomplete">
                {autocompleteHits.map((hit) => (
                  <li key={hit.path}>
                    <button type="button" onMouseDown={(e) => { e.preventDefault(); pickAutocomplete(hit.title); }}>
                      {hit.title}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {note.generated.trim() && (
            <div className="fs-brain__generated" data-testid="brain-generated-zone">
              <p className="fs-brain__generated-label">{t('Generated — read-only, replaced on the next sync')}</p>
              <pre className="fs-brain__generated-source">{note.generated}</pre>
            </div>
          )}
        </div>
      )}

      <Dialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t('Delete this note?')}
        description={t('It moves to the trash and can be restored from there.')}
        testId="brain-delete-dialog"
        footer={
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirmDelete(false)} />
            <Button
              variant="danger-solid"
              size="sm"
              label={t('Delete')}
              onClick={() => {
                setConfirmDelete(false);
                onDeleted();
              }}
              testId="brain-delete-confirm"
            />
          </>
        }
      />
    </section>
  );
}
