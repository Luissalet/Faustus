import { ArrowUp, Check, Folder } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Dialog, Skeleton } from '../../components';
import { browseWorkspace, vetWorkspace, type BrowseResult } from '../../adapters/composer';

/**
 * Folder picker. Browses the server's filesystem through
 * /api/workspace/browse — the same endpoint, the same admin gate — and hands
 * back a vetted path. Binding a folder is the central act of agent mode, so
 * this is a dialog with the folder tree in it, not a text field you have
 * to know the path for.
 */
export default function WorkspaceDialog({
  open,
  initial,
  onClose,
  onPick,
}: {
  open: boolean;
  initial: string;
  onClose: () => void;
  onPick: (path: string) => void;
}) {
  const [listing, setListing] = useState<BrowseResult | null>(null);
  const [typed, setTyped] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const go = (path: string) => {
    setListing(null);
    setError(null);
    browseWorkspace(path)
      .then((result) => {
        setListing(result);
        setTyped(result.path);
      })
      .catch((e: Error) => setError(e.message.includes('403') ? 'Elegir carpeta es solo para administradores.' : e.message));
  };

  useEffect(() => {
    if (open) go(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const choose = async (path: string) => {
    setBusy(true);
    setError(null);
    try {
      const vetted = await vetWorkspace(path);
      if (!vetted) {
        setError('Esa ruta no vale como carpeta de trabajo (no existe, es un fichero, o es una raíz protegida).');
        return;
      }
      onPick(vetted);
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title="Carpeta de trabajo"
      description="Las herramientas de ficheros y la terminal del agente quedan confinadas a esta carpeta."
      testId="workspace-dialog"
      footer={
        <>
          <Button label="Cancelar" onClick={onClose} />
          <Button
            variant="primary"
            icon={Check}
            label="Usar esta carpeta"
            loading={busy}
            disabled={!listing || !listing.selectable}
            onClick={() => listing && void choose(listing.path)}
            testId="workspace-use"
          />
        </>
      }
    >
      <form
        className="fs-ws__path"
        onSubmit={(event) => {
          event.preventDefault();
          if (typed.trim()) go(typed.trim());
        }}
      >
        <label className="fs-search" style={{ flex: 1, minInlineSize: 0 }}>
          <Folder size={14} aria-hidden="true" />
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            aria-label="Ruta de la carpeta"
            placeholder="D:\proyectos\mi-app"
            data-testid="workspace-path"
          />
        </label>
        <Button label="Ir" type="submit" size="sm" />
      </form>

      {error && (
        <p className="fs-notice" data-tone="danger">
          {error}
        </p>
      )}
      {!listing && !error && <Skeleton label="Leyendo carpetas" count={5} height="32px" />}
      {listing && (
        <div className="fs-ws__list" role="list">
          {listing.parent && (
            <button type="button" className="fs-ws__row" role="listitem" onClick={() => go(listing.parent as string)}>
              <ArrowUp size={14} aria-hidden="true" />
              <span>Subir un nivel</span>
            </button>
          )}
          {listing.dirs.map((dir) => (
            <button
              key={dir.path}
              type="button"
              className="fs-ws__row"
              role="listitem"
              onClick={() => go(dir.path)}
              onDoubleClick={() => void choose(dir.path)}
              data-testid="workspace-dir"
            >
              <Folder size={14} aria-hidden="true" />
              <span>{dir.name}</span>
            </button>
          ))}
          {listing.dirs.length === 0 && <p className="fs-studio__hint">Esta carpeta no tiene subcarpetas.</p>}
          {listing.truncated && <p className="fs-studio__hint">Lista recortada: hay más carpetas de las que enseño.</p>}
          {!listing.selectable && (
            <p className="fs-notice" data-tone="warning">
              Esta carpeta se puede recorrer pero no elegir; entra en una subcarpeta.
            </p>
          )}
        </div>
      )}
    </Dialog>
  );
}
