import { ArrowUp, Check, Folder, FolderOpen } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Button, Dialog, Skeleton } from '../../components';
import { browseWorkspace, pickNative, vetWorkspace, type BrowseResult } from '../../adapters/composer';
import { t } from '../../i18n';

/**
 * Folder picker, fallback edition. The chip tries the OS's own dialog first
 * (`pickNative`); this in-page browser only appears when that cannot open —
 * a browser on another machine, a headless host — and still offers a button
 * to retry the native one. Browses the server's filesystem through
 * /api/workspace/browse — the same endpoint, the same admin gate — and hands
 * back a vetted path.
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
      .catch((e: Error) => setError(e.message.includes('403') ? t('Choosing a folder is for administrators only.') : e.message));
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
        setError(t('That path will not do as a working folder (it does not exist, is a file, or is a protected root).'));
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

  const [nativeBusy, setNativeBusy] = useState(false);
  const openNative = async () => {
    setNativeBusy(true);
    setError(null);
    try {
      const res = await pickNative('folder', listing?.path || initial);
      if (res.status === 'ok' && res.path) {
        onPick(res.path);
        onClose();
      } else if (res.status === 'unavailable') {
        setError(
          t('The system file browser is not available from here (the browser has to be on the same machine as Faustus). Pick the folder from this list.'),
        );
      } else if (res.detail) {
        setError(res.detail);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setNativeBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={t('Working folder')}
      description={t('The agent\'s file tools and terminal are confined to this folder.')}
      testId="workspace-dialog"
      footer={
        <>
          <Button label={t('Cancel')} onClick={onClose} />
          <Button
            variant="primary"
            icon={Check}
            label={t('Use this folder')}
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
            aria-label={t('Folder path')}
            placeholder="D:\projects\my-app"
            data-testid="workspace-path"
          />
        </label>
        <Button label={t('Go')} type="submit" size="sm" />
        <Button
          label={t('System file browser…')}
          icon={FolderOpen}
          size="sm"
          loading={nativeBusy}
          onClick={() => void openNative()}
          testId="workspace-native"
        />
      </form>

      {error && (
        <p className="fs-notice" data-tone="danger">
          {error}
        </p>
      )}
      {!listing && !error && <Skeleton label={t('Reading folders')} count={5} height="32px" />}
      {listing && (
        <div className="fs-ws__list" role="list">
          {listing.parent && (
            <button type="button" className="fs-ws__row" role="listitem" onClick={() => go(listing.parent as string)}>
              <ArrowUp size={14} aria-hidden="true" />
              <span>{t('Up one level')}</span>
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
          {listing.dirs.length === 0 && <p className="fs-studio__hint">{t('This folder has no subfolders.')}</p>}
          {listing.truncated && <p className="fs-studio__hint">{t('List truncated: there are more folders than shown.')}</p>}
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
