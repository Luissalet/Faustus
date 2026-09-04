import { Archive, Download, Star, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { Button, Dialog } from '../../components';
import type { ChatSession } from '../../adapters/chat';
import {
  archiveSession,
  deleteSession,
  EXPORT_FORMATS,
  exportUrl,
  renameSession,
  setSessionImportant,
} from '../../adapters/sessions';

/**
 * The actions the old sidebar's row menu had — rename, favourite, archive,
 * export in six formats, delete — in one dialog per conversation. A lazy
 * chunk: it opens rarely and the eager bundle has a budget.
 */
export default function SessionDialog({
  target,
  currentId,
  onClose,
  onOpen,
  onChanged,
  onNotice,
}: {
  target: ChatSession;
  currentId: string | null;
  onClose: () => void;
  onOpen: (id: string | null) => void;
  onChanged: () => void;
  onNotice: (text: string, tone?: 'info' | 'warning' | 'danger') => void;
}) {
  const [name, setName] = useState(target.name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);

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
            if (name.trim() && name.trim() !== target.name) void act('Renombrar', () => renameSession(target.id, name.trim()));
          }}
        >
          <label className="fs-search" style={{ flex: 1, minInlineSize: 0 }}>
            <input value={name} onChange={(event) => setName(event.target.value)} aria-label="Nombre de la conversación" data-testid="session-rename" />
          </label>
          <Button label="Renombrar" type="submit" size="sm" disabled={busy || !name.trim() || name.trim() === target.name} />
        </form>

        <div className="fs-studio__ask-actions">
          <Button
            icon={Star}
            label={target.isImportant ? 'Quitar de favoritas' : 'Favorita'}
            size="sm"
            disabled={busy}
            onClick={() => void act('Favorita', () => setSessionImportant(target.id, !target.isImportant))}
          />
          <Button icon={Archive} label="Archivar" size="sm" disabled={busy} onClick={() => void act('Archivar', () => archiveSession(target.id))} />
        </div>

        <p className="fs-panel__label" style={{ margin: 0 }}>
          Exportar
        </p>
        <div className="fs-studio__ask-actions">
          {EXPORT_FORMATS.map((fmt) => (
            <a key={fmt} className="fs-btn" data-variant="secondary" data-size="sm" href={exportUrl(target.id, fmt)} download data-testid={`export-${fmt}`}>
              <Download size={14} aria-hidden="true" />
              <span>{fmt.toUpperCase()}</span>
            </a>
          ))}
        </div>

        {!confirmDelete ? (
          <div className="fs-studio__ask-actions">
            <Button variant="danger" icon={Trash2} label="Borrar conversación" size="sm" disabled={busy} onClick={() => setConfirmDelete(true)} />
          </div>
        ) : (
          <div className="fs-studio__ask" data-testid="session-delete-confirm">
            <p className="fs-prose">Se borra con todos sus mensajes. No se puede deshacer.</p>
            <div className="fs-studio__ask-actions">
              <Button
                variant="danger-solid"
                icon={Trash2}
                label="Sí, borrar"
                size="sm"
                loading={busy}
                onClick={() =>
                  void act('Borrar', async () => {
                    await deleteSession(target.id);
                    if (target.id === currentId) onOpen(null);
                  })
                }
              />
              <Button label="No" size="sm" onClick={() => setConfirmDelete(false)} />
            </div>
          </div>
        )}
      </div>
    </Dialog>
  );
}
