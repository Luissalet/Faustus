import { Download, Upload } from 'lucide-react';
import { useRef, useState, type ChangeEvent } from 'react';
import { Button } from '../../components';
import { canvasFileText, exportCanvas, importCanvas, type GraphScope } from '../../adapters/brain';
import { t } from '../../i18n';

/**
 * «Exportar a Canvas» / «Importar Canvas» over the graph (`/api/brain/canvas/*`,
 * src/brain/canvas.py). Export takes exactly what the graph shows — scope,
 * the local centre and depth, the kinds left on in the legend — saves it in
 * the vault's Canvas folder and downloads the same `.canvas`. Import reads a
 * `.canvas` the person picks, writes it into the vault as notes and links
 * (idempotent: the same file twice changes nothing) and reloads the graph.
 */
export function CanvasActions({ scope, center, depth, kinds, onImported }: {
  scope: GraphScope;
  center?: string;
  depth?: number;
  kinds?: string[];
  onImported: () => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<'export' | 'import' | null>(null);
  const [message, setMessage] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null);

  async function doExport() {
    setBusy('export');
    setMessage(null);
    try {
      const res = await exportCanvas({ scope, center, depth, kinds, save: true });
      const blob = new Blob([canvasFileText(res.canvas)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = res.fileName;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 2000);
      setMessage({
        tone: res.errors.length ? 'error' : 'ok',
        text: t('Exported {nodes} nodes and {edges} edges to {path}.', { nodes: res.nodes, edges: res.edges, path: res.path ?? res.fileName })
          + (res.truncated ? ` ${t('The graph was larger: narrow it to export the rest.')}` : ''),
      });
    } catch (e) {
      setMessage({ tone: 'error', text: t('Could not export: {error}', { error: String((e as Error)?.message ?? e) }) });
    } finally {
      setBusy(null);
    }
  }

  async function onFile(event: ChangeEvent<HTMLInputElement>) {
    const input = event.target;
    const file = input.files?.[0];
    if (!file) return;
    setBusy('import');
    setMessage(null);
    try {
      const text = await file.text();
      const name = file.name.replace(/\.(canvas|json)$/i, '') || 'Canvas';
      const res = await importCanvas(text, name);
      setMessage({
        tone: 'ok',
        text: t('Imported «{name}» into {folder}: {created} new notes, {updated} updated, {unchanged} unchanged, {linked} joined to existing notes, {edges} links.', {
          name: res.name, folder: res.folder, created: res.created, updated: res.updated, unchanged: res.unchanged,
          linked: res.linkedExisting, edges: res.edgesWritten + res.edgesViaIndex,
        }),
      });
      onImported();
    } catch (e) {
      setMessage({ tone: 'error', text: t('Could not import: {error}', { error: String((e as Error)?.message ?? e) }) });
    } finally {
      input.value = '';
      setBusy(null);
    }
  }

  return (
    <div className="fs-brain__canvas-actions">
      <Button variant="ghost" size="sm" icon={Download} label={t('Export to Canvas')} loading={busy === 'export'} disabled={busy !== null} onClick={doExport} testId="brain-canvas-export" />
      <Button variant="ghost" size="sm" icon={Upload} label={t('Import Canvas')} loading={busy === 'import'} disabled={busy !== null} onClick={() => fileRef.current?.click()} testId="brain-canvas-import" />
      <input ref={fileRef} type="file" accept=".canvas,.json,application/json" hidden onChange={onFile} data-testid="brain-canvas-file" />
      {message && (
        <span className="fs-brain__canvas-msg" data-tone={message.tone} role="status" data-testid="brain-canvas-message">
          {message.text}
        </span>
      )}
    </div>
  );
}
