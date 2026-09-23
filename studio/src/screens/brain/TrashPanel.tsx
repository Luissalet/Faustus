import { RotateCcw, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { EmptyState, IconButton, Skeleton } from '../../components';
import { loadTrash, restoreTrash, type TrashItem } from '../../adapters/brain';
import { locale, t } from '../../i18n';

/** Deleted notes: what a sync suppressed/removed/hid, one row per entry,
 *  restorable in one click (`notes.restore`, which un-suppresses or re-adds
 *  the source and rewrites the file). Never lists anything under `.trash/`
 *  itself as a browsable note — this panel is the only way in. */
export function TrashPanel({ onClose, onRestored }: { onClose: () => void; onRestored: (path: string) => void }) {
  const [items, setItems] = useState<TrashItem[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState('');

  function reload() {
    setError(null);
    loadTrash().then(setItems).catch(setError);
  }
  useEffect(reload, []);

  async function restore(id: string) {
    setBusy(id);
    try {
      const note = await restoreTrash(id);
      reload();
      onRestored(note.path);
    } catch (e) {
      setError(e);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="fs-brain__drawer-backdrop" role="presentation">
      <button type="button" className="fs-brain__scrim" aria-label={t('Close')} tabIndex={-1} onClick={onClose} />
      <aside className="fs-brain__drawer" role="dialog" aria-modal="true" aria-label={t('Trash')} onClick={(e) => e.stopPropagation()} data-testid="brain-trash-panel">
        <header className="fs-brain__drawer-head">
          <h3>{t('Trash')}</h3>
          <IconButton icon={X} label={t('Close')} size="sm" onClick={onClose} />
        </header>
        {!items && !error && <Skeleton label={t('Reading the trash')} count={3} height="40px" />}
        {error != null && <p className="fs-notice" data-tone="danger">{String((error as Error)?.message ?? error)}</p>}
        {items && items.length === 0 && <EmptyState title={t('Nothing in the trash')} body={t('Deleted notes, and anything a sync removed on the other side, land here.')} headingLevel={3} />}
        {items && items.length > 0 && (
          <ul className="fs-brain__trash-list">
            {items.map((item) => (
              <li key={item.id}>
                <div>
                  <strong>{item.title}</strong>
                  <p className="fs-muted">
                    {item.path} · {item.effect} · {new Date(item.deletedAt).toLocaleString(locale())}
                  </p>
                </div>
                <IconButton icon={RotateCcw} label={t('Restore')} size="sm" onClick={() => void restore(item.id)} disabled={busy === item.id} testId={`brain-restore-${item.id}`} />
              </li>
            ))}
          </ul>
        )}
      </aside>
    </div>
  );
}
