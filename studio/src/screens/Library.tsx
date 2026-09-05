import { Check, Library as LibraryIcon, Search } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { EmptyState, Skeleton, Toast } from '../components';
import { ImageGallery } from './library/Gallery';
import { DocumentsLibrary } from './library/Documents';
import { ChatsLibrary } from './library/Chats';
import { ResearchLibrary } from './library/Research';
import { loadLibrary, type Artifact } from '../adapters/library';
import { relativeTime } from '../adapters/home';
import { useSpotlight } from '../shell/useSpotlight';
import './projects.css';
import './home.css';
import './library.css';
import { t, tn } from '../i18n';

const TYPES = [
  { id: 'todo', label: 'All' },
  { id: 'imagen', label: 'Images' },
  { id: 'documento', label: 'Documents' },
  { id: 'chats', label: 'Chats' },
  { id: 'research', label: 'Research' },
  { id: 'archivo', label: 'Archive' },
];

const ARCHIVE_KINDS = [
  { id: 'documento', label: 'Documents' },
  { id: 'chats', label: 'Chats' },
  { id: 'research', label: 'Research' },
];

/**
 * Biblioteca (UI-041).
 *
 * Gallery and documents in one place, because "find the thing I made" should
 * not require remembering which subsystem made it. Filters live in the URL,
 * so a filtered view is a link.
 */
export function LibraryScreen() {
  const spotlight = useSpotlight();
  const [params, setParams] = useSearchParams();
  const [artifacts, setArtifacts] = useState<Artifact[] | null>(null);
  const [degraded, setDegraded] = useState<string[]>([]);
  const [failed, setFailed] = useState(false);

  const type = params.get('type') ?? 'todo';
  const query = params.get('q') ?? '';
  const archiveKind = params.get('kind') ?? 'documento';
  const federated = type === 'todo';
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);
  const say = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2600);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    loadLibrary(controller.signal)
      .then((result) => {
        setArtifacts(result.artifacts);
        setDegraded(result.degraded);
      })
      .catch(() => setFailed(true));
    return () => controller.abort();
  }, []);

  const visible = useMemo(() => {
    if (!artifacts) return [];
    const needle = query.trim().toLowerCase();
    return artifacts.filter((artifact) => {
      if (type !== 'todo' && artifact.kind !== type) return false;
      if (!needle) return true;
      return [artifact.title, artifact.subtitle, artifact.session, artifact.meta]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(needle));
    });
  }, [artifacts, type, query]);

  function setParam(key: string, value: string, fallback: string) {
    const next = new URLSearchParams(params);
    if (value === fallback) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: key === 'q' });
  }

  if (failed) {
    return (
      <EmptyState
        icon={LibraryIcon}
        title={t('Could not read the library')}
        body={t('Neither the gallery nor the documents responded.')}
        primaryAction={{ label: t('Retry'), onClick: () => window.location.reload() }}
      />
    );
  }

  return (
    <div className="fs-screen" data-testid="library">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Library')}</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {artifacts ? `${tn(artifacts.length, '{n} artefact', '{n} artefacts')}. ${t('Images and documents, whichever subsystem created them.')}` : t('Images and documents, whichever subsystem created them.')}
          </p>
        </div>
        <label className="fs-search">
          <Search size={15} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder={t('Search by text, session or model')}
            aria-label={t('Search the library')}
            data-testid="library-search"
            onChange={(event) => setParam('q', event.target.value, '')}
          />
        </label>
      </header>

      <div className="fs-tabs" role="tablist" aria-label={t('Filter by type')}>
        {TYPES.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={entry.id === type}
            className="fs-tab"
            data-testid={`library-type-${entry.id}`}
            onClick={() => setParam('type', entry.id, 'todo')}
          >
            {t(entry.label)}
          </button>
        ))}
      </div>

      {type === 'imagen' && <ImageGallery query={query} say={say} />}
      {type === 'documento' && <DocumentsLibrary query={query} say={say} />}
      {type === 'chats' && <ChatsLibrary query={query} say={say} />}
      {type === 'research' && <ResearchLibrary query={query} say={say} />}
      {type === 'archivo' && (
        <>
          <div className="fs-gal__chips fs-lib__kinds" role="group" aria-label={t('What to show from the archive')}>
            {ARCHIVE_KINDS.map((k) => (
              <button key={k.id} type="button" className="fs-chip" data-on={archiveKind === k.id || undefined} onClick={() => setParam('kind', k.id, 'documento')}>
                {t(k.label)}
              </button>
            ))}
          </div>
          {archiveKind === 'documento' && <DocumentsLibrary query={query} say={say} archived />}
          {archiveKind === 'chats' && <ChatsLibrary query={query} say={say} archived />}
          {archiveKind === 'research' && <ResearchLibrary query={query} say={say} archived />}
        </>
      )}

      {federated && !artifacts && (
        <div className="fs-grid">
          {[0, 1, 2, 3, 4, 5].map((index) => (
            <Skeleton key={index} label={t('Loading the library')} height="200px" radius="panel" />
          ))}
        </div>
      )}

      {federated && artifacts && visible.length === 0 && (
        <EmptyState
          icon={LibraryIcon}
          title={
            query || type !== 'todo' ? t('Nothing matches the filter') : t('The library is empty')
          }
          body={
            query || type !== 'todo'
              ? t('Try another text, or another kind of artefact.')
              : t('When you generate an image or write a document they will appear here, with the session that produced them and their recipe.')
          }
        />
      )}

      {federated && artifacts && visible.length > 0 && (
        <div className="fs-grid">
          {visible.map((artifact) => (
            <article
              className="fs-card fs-spot"
              onMouseMove={spotlight}
              key={artifact.id}
              data-testid="library-card"
            >
              {artifact.kind === 'imagen' && artifact.imageUrl ? (
                <img
                  className="fs-card__preview"
                  src={artifact.imageUrl}
                  alt={artifact.title}
                  loading="lazy"
                  decoding="async"
                  /* Explicit dimensions: without them every image that loads
                     shoves the grid down, which is the layout shift the
                     performance budget forbids. */
                  width={artifact.width ?? undefined}
                  height={artifact.height ?? undefined}
                />
              ) : (
                <p className="fs-card__doc">
                  {artifact.subtitle || t('Document with no content yet.')}
                </p>
              )}
              <div className="fs-card__body">
                <span className="fs-card__kind">{artifact.kind}</span>
                {artifact.kind === 'documento' ? (
                  <Link className="fs-card__title fs-link" to={`/documents/${encodeURIComponent(artifact.id.replace(/^doc-/, ''))}`} title={t('Open in the editor')}>
                    {artifact.title}
                  </Link>
                ) : (
                  <Link className="fs-card__title fs-link" to={`/library?type=imagen&img=${encodeURIComponent(artifact.id.replace(/^img-/, ''))}`} title={t('Open the image')}>
                    {artifact.title}
                  </Link>
                )}
                <span className="fs-card__meta">
                  {[artifact.meta, artifact.session, relativeTime(artifact.createdAt)]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </div>
            </article>
          ))}
        </div>
      )}

      {federated && degraded.length > 0 && (
        <p className="fs-notice" data-tone="warning">
          {t('Could not read {what}. What you see is real; that part is missing.', { what: degraded.join(', ') })}
        </p>
      )}

      {notice && (
        <Toast>
          <Check size={12} aria-hidden="true" /> {notice}
        </Toast>
      )}
    </div>
  );
}
