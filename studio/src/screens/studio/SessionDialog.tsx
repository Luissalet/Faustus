import { Archive, Copy, Download, FolderOpen, Star, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Button, Dialog } from '../../components';
import type { ChatSession } from '../../adapters/chat';
import {
  archiveSession,
  deleteSession,
  EXPORT_FORMATS,
  downloadExport,
  forkSession,
  renameSession,
  setSessionFolder,
  setSessionImportant,
} from '../../adapters/sessions';
import { t } from '../../i18n';

/**
 * The actions the old sidebar's row menu had — rename, favourite, archive,
 * move to a folder, duplicate, export in six formats, delete — in one
 * dialog per conversation. A lazy chunk: it opens rarely and the eager
 * bundle has a budget.
 */
export default function SessionDialog({
  target,
  currentId,
  folders = [],
  onClose,
  onOpen,
  onChanged,
  onNotice,
}: {
  target: ChatSession;
  currentId: string | null;
  /** Folder names in use, for "move to". */
  folders?: string[];
  onClose: () => void;
  onOpen: (id: string | null) => void;
  onChanged: () => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
}) {
  const [name, setName] = useState(target.name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [folder, setFolder] = useState(target.folder ?? '');
  const [newFolder, setNewFolder] = useState('');

  const act = async (what: string, fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
      onChanged();
      onClose();
    } catch (error) {
      onNotice(`${what}: ${(error as Error).message}`, 'danger');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={target.name}
      testId="session-dialog"
    >
      <div className="fs-session-dialog">
        <form
          className="fs-ws__path"
          onSubmit={(event) => {
            event.preventDefault();
            if (name.trim() && name.trim() !== target.name) void act(t('Rename'), () => renameSession(target.id, name.trim()));
          }}
        >
          <label className="fs-search" style={{ flex: 1, minInlineSize: 0 }}>
            <input value={name} onChange={(event) => setName(event.target.value)} aria-label={t('Conversation name')} data-testid="session-rename" />
          </label>
          <Button label={t('Rename')} type="submit" size="sm" disabled={busy || !name.trim() || name.trim() === target.name} />
        </form>

        <div className="fs-studio__ask-actions">
          <Button
            icon={Star}
            label={target.isImportant ? t('Remove from favourites') : t('Favourite')}
            size="sm"
            disabled={busy}
            onClick={() => void act(t('Favourite'), () => setSessionImportant(target.id, !target.isImportant))}
          />
          <Button icon={Archive} label={t('Archive')} size="sm" disabled={busy} onClick={() => void act(t('Archive'), () => archiveSession(target.id))} />
          <Button
            icon={Copy}
            label={t('Duplicate')}
            size="sm"
            disabled={busy}
            onClick={() =>
              void act(t('Duplicate'), async () => {
                const copy = await forkSession(target.id, target.messageCount || 10000);
                onNotice(t('Duplicated as "{name}".', { name: copy.name }));
                onOpen(copy.id);
              })
            }
          />
        </div>

        <p className="fs-panel__label" style={{ margin: 0 }}>
          {t('Folder')}
        </p>
        <form
          className="fs-ws__path"
          data-testid="session-folder"
          onSubmit={(event) => {
            event.preventDefault();
            const next = (newFolder.trim() || folder).trim();
            if (next !== (target.folder ?? '')) void act(t('Folder'), () => setSessionFolder(target.id, next));
          }}
        >
          <label className="fs-search" style={{ flex: 1, minInlineSize: 0 }}>
            <FolderOpen size={14} aria-hidden="true" />
            <select value={folder} onChange={(e) => setFolder(e.target.value)} aria-label={t('Folder')} className="fs-select">
              <option value="">{t('No folder')}</option>
              {folders.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </label>
          <label className="fs-search" style={{ flex: 1, minInlineSize: 0 }}>
            <input value={newFolder} onChange={(e) => setNewFolder(e.target.value)} placeholder={t('or a new one…')} aria-label={t('New folder')} />
          </label>
          <Button label={t('Move')} type="submit" size="sm" disabled={busy || (newFolder.trim() || folder) === (target.folder ?? '')} />
        </form>

        <p className="fs-panel__label" style={{ margin: 0 }}>
          {t('Export')}
        </p>
        <div className="fs-studio__ask-actions">
          {EXPORT_FORMATS.map((fmt) => (
            <Button
              key={fmt}
              variant="secondary"
              size="sm"
              icon={Download}
              label={fmt.toUpperCase()}
              disabled={busy}
              testId={`export-${fmt}`}
              onClick={() => {
                // Not a plain link: a link cannot see a 400 or a 503, so a
                // refused export used to be a blank tab.
                void downloadExport(target.id, fmt, target.name).catch((e: Error) =>
                  onNotice(`${t('Could not export')}: ${e.message}`, 'danger'),
                );
              }}
            />
          ))}
        </div>

        {!confirmDelete ? (
          <div className="fs-studio__ask-actions">
            <Button variant="danger" icon={Trash2} label={t('Delete conversation')} size="sm" disabled={busy} onClick={() => setConfirmDelete(true)} />
          </div>
        ) : (
          <div className="fs-studio__ask" data-testid="session-delete-confirm">
            <p className="fs-prose">{t('It is deleted with all its messages. This cannot be undone.')}</p>
            <div className="fs-studio__ask-actions">
              <Button
                variant="danger-solid"
                icon={Trash2}
                label={t('Yes, delete')}
                size="sm"
                loading={busy}
                onClick={() =>
                  void act(t('Delete'), async () => {
                    await deleteSession(target.id);
                    if (target.id === currentId) onOpen(null);
                  })
                }
              />
              <Button label={t(t('No'))} size="sm" onClick={() => setConfirmDelete(false)} />
            </div>
          </div>
        )}
      </div>
    </Dialog>
  );
}
