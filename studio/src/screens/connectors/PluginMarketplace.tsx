import { useCallback, useEffect, useState } from 'react';
import { Download, ExternalLink, Folder, RefreshCw } from 'lucide-react';
import { Button, Skeleton } from '../../components';
import {
  installMarketplacePlugin, linkMarketplacePlugin, listMarketplace, unlinkMarketplacePlugin,
  type MarketplaceCatalogue, type MarketplacePlugin,
} from '../../adapters/plugin-marketplace';
import { t } from '../../i18n';

function sourceLabel(row: MarketplacePlugin) {
  if (row.state === 'linked') return t('Linked folder');
  if (row.state === 'cloned') return t('Source downloaded');
  if (row.state === 'invalid_link') return t('Linked folder unavailable');
  if (row.state === 'conflict') return t('Check existing folder');
  return t('Not downloaded');
}

export function PluginMarketplace({ onConfigure }: {
  onConfigure?: (id: string, path: string) => void;
}) {
  const [catalogue, setCatalogue] = useState<MarketplaceCatalogue | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [linking, setLinking] = useState<string | null>(null);
  const [path, setPath] = useState('');
  const reload = useCallback(async () => {
    const result = await listMarketplace();
    setCatalogue(result);
    setError('');
  }, []);

  useEffect(() => { void reload().catch((e: Error) => setError(e.message)); }, [reload]);

  const change = async (id: string, operation: () => Promise<unknown>, message: string) => {
    setBusy(id);
    setError('');
    setNotice('');
    try {
      await operation();
      setNotice(message);
      setLinking(null);
      setPath('');
      await reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const needle = query.trim().toLocaleLowerCase();
  const rows = (catalogue?.plugins ?? []).filter((row) => row.required ||
    `${row.name} ${row.purpose}`.toLocaleLowerCase().includes(needle));

  return (
    <section id="plugin-marketplace-panel" className="fs-marketplace" aria-labelledby="plugin-marketplace-title" data-testid="plugin-marketplace">
      <div className="fs-conn__row-main">
        <div>
          <h2 id="plugin-marketplace-title" className="fs-set__card-title">{t('Plugin marketplace')}</h2>
          <p className="fs-prose">{t('HoardLink is required. Choose the Hoards you want; missing repositories download into your plugins folder.')}</p>
        </div>
        <Button size="sm" variant="ghost" icon={RefreshCw} label={t('Refresh')}
          disabled={!!busy} onClick={() => void reload().catch((e: Error) => setError(e.message))} />
      </div>
      <p className="fs-set__help">{t('Already have a Hoard elsewhere? Link its folder. Downloading saves the source; configure and start the app separately.')}</p>
      <label className="fs-marketplace__search">
        <span>{t('Find a plugin')}</span>
        <input className="fs-field" type="search" value={query} onChange={(event) => setQuery(event.target.value)} />
      </label>
      {error && <div className="fs-notice" role="alert">{error}</div>}
      {notice && <p className="fs-set__help" role="status">{notice}</p>}
      {!catalogue ? (error ? <Button variant="secondary" label={t('Try again')}
        onClick={() => void reload().catch((e: Error) => setError(e.message))} /> : <Skeleton label={t('Loading')} count={2} height="72px" />) : (
        <ul className="fs-conn__list">
          {rows.map((row) => (
            <li className="fs-conn__row" key={row.id} data-testid={`marketplace-${row.id}`}>
              <div className="fs-conn__row-main">
                <div className="fs-conn__identity">
                  <strong>{row.id === 'hoardhub' ? `HoardLink (${row.name})` : row.name}</strong>
                  <span className="fs-set__help">{t(row.purpose)}</span>
                </div>
                <span className="fs-conn-status">{row.required ? `${t('Required')} · ` : ''}{sourceLabel(row)}</span>
              </div>
              {row.local_path && <p className="fs-marketplace__path"><Folder size={14} aria-hidden="true" />{row.local_path}</p>}
              {row.error && <p className="fs-set__help">{row.error}</p>}
              <div className="fs-conn__actions">
                {row.can_install && <Button size="sm" variant={row.required ? 'primary' : 'secondary'}
                  icon={Download} label={t('Download')} loading={busy === row.id} disabled={!!busy}
                  onClick={() => void change(row.id, () => installMarketplacePlugin(row.id), t('Source downloaded.'))} />}
                {row.local_path && (row.state === 'linked' || row.state === 'cloned') && onConfigure &&
                  <Button size="sm" variant="secondary" label={t('Configure connector')} disabled={!!busy}
                    onClick={() => onConfigure(row.id, row.local_path!)} />}
                <Button size="sm" variant="ghost" label={t('Link existing folder')} disabled={!!busy}
                  onClick={() => { setLinking(row.id); setPath(row.local_path ?? ''); }} />
                {row.source_kind === 'linked' && <Button size="sm" variant="ghost" label={t('Forget link')} disabled={!!busy}
                  onClick={() => void change(row.id, () => unlinkMarketplacePlugin(row.id), t('Link removed; files kept.'))} />}
                <a className="fs-link" href={row.repository_url.replace(/\.git$/, '')} target="_blank" rel="noopener noreferrer">
                  <ExternalLink size={13} aria-hidden="true" />{t('Original repository')}
                </a>
              </div>
              {linking === row.id && <form className="fs-marketplace__link" onSubmit={(event) => {
                event.preventDefault();
                if (path.trim()) void change(row.id, () => linkMarketplacePlugin(row.id, path.trim()), t('Folder linked.'));
              }}>
                <label htmlFor={`plugin-path-${row.id}`}>{t('Existing repository folder')}</label>
                <input id={`plugin-path-${row.id}`} className="fs-field" value={path} autoFocus
                  onChange={(event) => setPath(event.target.value)} disabled={!!busy} />
                <div className="fs-conn__actions">
                  <Button size="sm" type="submit" label={t('Link folder')} disabled={!!busy || !path.trim()} />
                  <Button size="sm" variant="ghost" label={t('Cancel')} disabled={!!busy} onClick={() => setLinking(null)} />
                </div>
              </form>}
            </li>
          ))}
          {rows.length === 0 && <li className="fs-set__help">{t('No plugins match your search.')}</li>}
        </ul>
      )}
    </section>
  );
}
