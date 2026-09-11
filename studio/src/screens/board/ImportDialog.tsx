import { useState } from 'react';
import { Button, Dialog } from '../../components';
import { importBoard, type BoardImportSource, type ImportResult } from '../../adapters/board';
import { t } from '../../i18n';

const SOURCES: { id: BoardImportSource; label: string; help: string }[] = [
  { id: 'objetivos', label: 'OBJETIVOS.md', help: 'Each "## OBJ-N" becomes a feature; already-closed ones import as done.' },
  { id: 'pendientes', label: 'PENDIENTES.md', help: 'Checklist lines only — struck-through/[x] import as done, active ones as open.' },
  { id: 'backlog', label: 'docs/spec/v2/backlog.json', help: 'Its own id becomes a label; dependencies become "blocks" links.' },
];

/**
 * Lote 93 — "Import from OBJETIVOS/PENDIENTES/backlog", contract §POST
 * /import: a dry run first (server reports what it WOULD do, nothing
 * written), then the same call for real. Idempotent server-side
 * (`import:<source>:<id>` labels), so running it again after new items
 * appear in the source files only creates what is new.
 */
export function ImportDialog({
  open,
  onOpenChange,
  projectId,
  onImported,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  onImported: () => void;
}) {
  const [sources, setSources] = useState<BoardImportSource[]>(['objetivos', 'pendientes', 'backlog']);
  const [preview, setPreview] = useState<ImportResult | null>(null);
  const [busy, setBusy] = useState<'preview' | 'import' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const toggle = (id: BoardImportSource) =>
    setSources((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]));

  const runPreview = async () => {
    if (!sources.length) return;
    setBusy('preview');
    setError(null);
    try {
      setPreview(await importBoard(projectId, sources, true));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const runImport = async () => {
    if (!sources.length) return;
    setBusy('import');
    setError(null);
    try {
      const result = await importBoard(projectId, sources, false);
      setPreview(result);
      onImported();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) {
          setPreview(null);
          setError(null);
        }
        onOpenChange(o);
      }}
      title={t('Import into the board')}
      testId="board-import-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => onOpenChange(false)} />
          <Button size="sm" label={t('Preview')} loading={busy === 'preview'} disabled={!sources.length} onClick={() => void runPreview()} testId="board-import-preview" />
          <Button variant="primary" size="sm" label={t('Import')} loading={busy === 'import'} disabled={!sources.length || !preview} onClick={() => void runImport()} testId="board-import-confirm" />
        </>
      }
    >
      <p className="fs-prose">{t('Existing markdown does not disappear; this copies what it describes into issues, without duplicating anything already imported.')}</p>
      <ul className="fs-issue-import__sources">
        {SOURCES.map((s) => (
          <li key={s.id}>
            <label>
              <input type="checkbox" checked={sources.includes(s.id)} onChange={() => toggle(s.id)} />
              <span>
                <strong>{s.label}</strong>
                <small className="fs-muted">{t(s.help)}</small>
              </span>
            </label>
          </li>
        ))}
      </ul>
      {error && <p className="fs-notice" data-tone="danger" role="alert">{error}</p>}
      {preview && (
        <div className="fs-issue-import__preview" data-testid="board-import-preview-result">
          <p>{t('{created} to create, {skipped} already imported.', { created: preview.created, skipped: preview.skipped })}</p>
          {preview.preview.length > 0 && (
            <ul>
              {preview.preview.slice(0, 30).map((row, i) => (
                <li key={i}>{row.id ? <code>{row.id}</code> : null} {row.title}</li>
              ))}
              {preview.preview.length > 30 && <li className="fs-muted">{t('+{n} more', { n: preview.preview.length - 30 })}</li>}
            </ul>
          )}
        </div>
      )}
    </Dialog>
  );
}
