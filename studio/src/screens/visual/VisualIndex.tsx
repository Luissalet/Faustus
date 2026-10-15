import { Eye, FileSearch, Plus, Search, Trash2 } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, EmptyState, Skeleton, Toast } from '../../components';
import * as api from '../../adapters/visualIndex';
import { t } from '../../i18n';
import './visualIndex.css';

/**
 * Visual index: collections of PDFs, pictures and web pages kept as screenshot strips, searched by meaning.
 *
 * A strip is a piece of a page, not its text. "Read" asks the resident vision model; when there is none, the server
 * says so and this screen shows that sentence instead of anything of its own. Reasoning lives in
 * ``adapters/visualIndex.ts`` (checked by ``studio/checks/visualIndex.check.mjs``).
 */

const message = (exc: unknown): string => (exc instanceof Error ? exc.message : String(exc));

export function VisualIndexScreen() {
  const [status, setStatus] = useState<api.IndexStatus | null>(null);
  const [collections, setCollections] = useState<api.CollectionSummary[]>([]);
  const [current, setCurrent] = useState('');
  const [sources, setSources] = useState<api.SourceRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [addText, setAddText] = useState('');
  const [addCollection, setAddCollection] = useState('default');
  const [addPages, setAddPages] = useState('');
  const [added, setAdded] = useState<api.AddRow[]>([]);

  const [query, setQuery] = useState('');
  const [scope, setScope] = useState('');
  const [hits, setHits] = useState<api.Hit[] | null>(null);
  const [readings, setReadings] = useState<Record<string, api.ReadResult | 'busy'>>({});
  const [question, setQuestion] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [st, list] = await Promise.all([api.loadStatus(), api.listCollections()]);
      setStatus(st);
      setCollections(list);
    } catch (exc) {
      setError(message(exc));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!current) {
      setSources([]);
      return;
    }
    let live = true;
    api.listSources(current).then((rows) => live && setSources(rows)).catch((exc) => live && setError(message(exc)));
    return () => {
      live = false;
    };
  }, [current, collections]);

  const ready = status ? api.readiness(status) : { ready: false, reason: '' };

  const onAdd = async () => {
    const list = api.parseSources(addText);
    if (!list.length) {
      setError(t('Give at least one path or URL'));
      return;
    }
    setBusy(true);
    setError(null);
    setAdded([]);
    try {
      setAdded(await api.addSources(list, addCollection.trim(), addPages));
      setCurrent(addCollection.trim() || 'default');
      await refresh();
      setToast(t('Indexing finished'));
    } catch (exc) {
      setError(message(exc));
    } finally {
      setBusy(false);
    }
  };

  const onSearch = async () => {
    if (!query.trim()) {
      setError(t('Write what you are looking for'));
      return;
    }
    setBusy(true);
    setError(null);
    setReadings({});
    try {
      setHits(await api.search(query.trim(), 6, scope));
    } catch (exc) {
      setError(message(exc));
    } finally {
      setBusy(false);
    }
  };

  const onRead = async (hit: api.Hit) => {
    setReadings((r) => ({ ...r, [hit.id]: 'busy' }));
    try {
      const out = await api.readTile(hit.id, question);
      setReadings((r) => ({ ...r, [hit.id]: out }));
    } catch (exc) {
      setReadings((r) => {
        const { [hit.id]: _drop, ...rest } = r;
        return rest;
      });
      setError(message(exc));
    }
  };

  const onRemoveSource = async (source: string) => {
    setBusy(true);
    try {
      await api.removeSource(current, source);
      await refresh();
    } catch (exc) {
      setError(message(exc));
    } finally {
      setBusy(false);
    }
  };

  const onRemoveCollection = async (name: string) => {
    setBusy(true);
    try {
      await api.removeCollection(name);
      if (current === name) setCurrent('');
      await refresh();
    } catch (exc) {
      setError(message(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-vix" data-screen="visual-index">
      <header className="fs-vix__head">
        <div>
          <h1>{t('Visual index')}</h1>
          <p>{t('Search PDFs, pictures and web pages by what they look like: tables, diagrams, charts, scans.')}</p>
        </div>
      </header>

      {status && !ready.ready ? (
        <div className="fs-vix__notice" role="note" data-reason={ready.reason}>
          {ready.reason === 'off'
            ? t('The visual index is switched off in Settings.')
            : `${t('The visual embedder is not installed')}: ${status.dependenciesMissing.join(', ')}. ${status.installHint}`}
        </div>
      ) : null}
      {status && ready.ready && ready.reason === 'download' ? (
        <div className="fs-vix__notice" role="note" data-reason="download">
          {t('The embedding model downloads the first time you index or search (several GB).')}
        </div>
      ) : null}
      {status ? (
        <p className="fs-vix__meta">
          {status.visionChecking
            ? t('Checking which vision model answers…')
            : status.visionAvailable
            ? `${t('Vision model')}: ${status.visionModel}`
            : `${t('No vision model')}: ${status.visionNote || t('strips can be searched but not read')}`}
          {status.device ? ` · ${status.device}` : ''}
        </p>
      ) : null}

      {error ? <div className="fs-vix__error" role="alert">{error}</div> : null}

      <div className="fs-vix__grid">
        <section className="fs-vix__panel" aria-labelledby="vix-collections">
          <h2 id="vix-collections">{t('Collections')}</h2>
          {loading ? <Skeleton label={t('Loading collections')} count={3} height="32px" /> : null}
          {!loading && collections.length === 0 ? (
            <EmptyState icon={FileSearch} title={t('No collections yet')} body={t('Add a PDF, a picture or a URL to start one.')} />
          ) : null}
          <ul className="fs-vix__list">
            {collections.map((c) => (
              <li key={c.name} data-current={c.name === current ? 'yes' : undefined}>
                <button type="button" className="fs-vix__link" onClick={() => setCurrent(c.name)}>
                  {c.name}
                  <span className="fs-vix__badge">{c.strips} {t('strips')}</span>
                </button>
                <button type="button" className="fs-vix__icon" aria-label={t('Delete collection')} onClick={() => void onRemoveCollection(c.name)}>
                  <Trash2 size={14} />
                </button>
              </li>
            ))}
          </ul>
          {current ? (
            <>
              <h3>{t('Sources in {name}').replace('{name}', current)}</h3>
              <ul className="fs-vix__list">
                {sources.map((s) => (
                  <li key={s.source}>
                    <span className="fs-vix__source" title={s.source}>{s.title || api.shortSource(s.source)}</span>
                    <span className="fs-vix__badge">{s.kind} · {s.strips}</span>
                    {!s.complete ? <span className="fs-vix__badge" data-warn="yes">{t('partial')}</span> : null}
                    <button type="button" className="fs-vix__icon" aria-label={t('Remove source')} onClick={() => void onRemoveSource(s.source)}>
                      <Trash2 size={14} />
                    </button>
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </section>

        <section className="fs-vix__panel" aria-labelledby="vix-add">
          <h2 id="vix-add">{t('Add to the index')}</h2>
          <label className="fs-vix__label">
            {t('Paths or URLs (one per line)')}
            <textarea value={addText} onChange={(e) => setAddText(e.target.value)} rows={5} spellCheck={false} placeholder="https://example.org/report.pdf" />
          </label>
          <div className="fs-vix__row">
            <label className="fs-vix__label">
              {t('Collection')}
              <input value={addCollection} onChange={(e) => setAddCollection(e.target.value)} spellCheck={false} />
            </label>
            <label className="fs-vix__label">
              {t('PDF pages')}
              <input value={addPages} onChange={(e) => setAddPages(e.target.value)} placeholder="1-5,9" spellCheck={false} />
            </label>
          </div>
          <Button label={t('Index')} onClick={() => void onAdd()} disabled={busy || !ready.ready} icon={Plus} />
          {added.length ? (
            <ul className="fs-vix__results">
              {added.map((r) => (
                <li key={r.source} data-ok={r.ok ? 'yes' : 'no'}>
                  <span title={r.source}>{api.shortSource(r.source, 48)}</span>
                  {r.ok
                    ? <span>{r.unchanged ? t('unchanged') : `${r.total} ${t('strips')} (${r.added} ${t('new')}, ${r.reused} ${t('reused')})`}</span>
                    : <span className="fs-vix__failed">{r.note}</span>}
                </li>
              ))}
            </ul>
          ) : null}
        </section>

        <section className="fs-vix__panel fs-vix__panel--wide" aria-labelledby="vix-search">
          <h2 id="vix-search">{t('Search')}</h2>
          <div className="fs-vix__row">
            <label className="fs-vix__label fs-vix__label--grow">
              {t('What are you looking for?')}
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') void onSearch(); }}
                placeholder={t('the sales chart for 2024')}
              />
            </label>
            <label className="fs-vix__label">
              {t('In')}
              <select value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="">{t('All collections')}</option>
                {collections.map((c) => <option key={c.name} value={c.name}>{c.name}</option>)}
              </select>
            </label>
            <Button label={t('Search')} onClick={() => void onSearch()} disabled={busy || !ready.ready} icon={Search} />
          </div>
          <label className="fs-vix__label">
            {t('Question for the vision model (optional)')}
            <input value={question} onChange={(e) => setQuestion(e.target.value)} placeholder={t('What is the value for 2024?')} />
          </label>
          {hits && hits.length === 0 ? <p className="fs-vix__hint">{t('No strips matched.')}</p> : null}
          <div className="fs-vix__hits">
            {(hits ?? []).map((hit) => {
              const reading = readings[hit.id];
              return (
                <article key={hit.id} className="fs-vix__hit">
                  <img src={api.tileImageUrl(hit.id)} alt={`${hit.title || hit.source} ${api.hitLocation(hit)}`} loading="lazy" />
                  <div className="fs-vix__hitmeta">
                    <strong title={hit.source}>{hit.title || api.shortSource(hit.source, 48)}</strong>
                    <span>{api.hitLocation(hit)} · {hit.score.toFixed(3)}</span>
                    {api.isUrl(hit.source) ? <a href={hit.source} target="_blank" rel="noreferrer noopener">{t('Open source')}</a> : null}
                    <Button variant="secondary" label={t('Read strip')} icon={Eye} onClick={() => void onRead(hit)} disabled={reading === 'busy'} />
                    {reading && reading !== 'busy' ? (
                      <p className="fs-vix__reading" data-vision={reading.visionAvailable ? 'yes' : 'no'}>
                        {reading.visionAvailable ? <em>{reading.answeredBy}: </em> : null}
                        {reading.text}
                      </p>
                    ) : null}
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      </div>

      {toast ? <Toast>{toast}</Toast> : null}
    </div>
  );
}

export default VisualIndexScreen;
