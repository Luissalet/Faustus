import { Library as LibraryIcon, Search } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { EmptyState, Skeleton } from '../components';
import { loadLibrary, type Artifact } from '../adapters/library';
import { relativeTime } from '../adapters/home';
import { useSpotlight } from '../shell/useSpotlight';
import './projects.css';
import './home.css';
import './library.css';

const TYPES = [
  { id: 'todo', label: 'Todo' },
  { id: 'imagen', label: 'Imágenes' },
  { id: 'documento', label: 'Documentos' },
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
        title="No he podido leer la biblioteca"
        body="Ni la galería ni los documentos han respondido. La interfaz anterior no depende de esta pantalla."
        primaryAction={{
          label: 'Abrir la interfaz anterior',
          onClick: () => {
            window.location.href = '/?shell=legacy';
          },
        }}
      />
    );
  }

  return (
    <div className="fs-screen" data-testid="library">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">Biblioteca</h1>
          <p className="fs-prose" style={{ marginBlockStart: 'var(--fs-space-2)' }}>
            {artifacts
              ? `${artifacts.length} ${artifacts.length === 1 ? 'artefacto' : 'artefactos'}, sin importar qué subsistema los creó.`
              : 'Imágenes y documentos, sin importar qué subsistema los creó.'}
          </p>
        </div>
        <label className="fs-search">
          <Search size={15} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder="Buscar por texto, sesión o modelo"
            aria-label="Buscar en la biblioteca"
            data-testid="library-search"
            onChange={(event) => setParam('q', event.target.value, '')}
          />
        </label>
      </header>

      <div className="fs-tabs" role="tablist" aria-label="Filtrar por tipo">
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
            {entry.label}
          </button>
        ))}
      </div>

      {!artifacts && (
        <div className="fs-grid">
          {[0, 1, 2, 3, 4, 5].map((index) => (
            <Skeleton key={index} label="Cargando la biblioteca" height="200px" radius="panel" />
          ))}
        </div>
      )}

      {artifacts && visible.length === 0 && (
        <EmptyState
          icon={LibraryIcon}
          title={
            query || type !== 'todo' ? 'Nada coincide con el filtro' : 'La biblioteca está vacía'
          }
          body={
            query || type !== 'todo'
              ? 'Prueba con otro texto, u otro tipo de artefacto.'
              : 'Cuando generes una imagen o escribas un documento aparecerán aquí, con la sesión que los produjo y su receta.'
          }
        />
      )}

      {artifacts && visible.length > 0 && (
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
                  {artifact.subtitle || 'Documento sin contenido todavía.'}
                </p>
              )}
              <div className="fs-card__body">
                <span className="fs-card__kind">{artifact.kind}</span>
                <span className="fs-card__title">{artifact.title}</span>
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

      {degraded.length > 0 && (
        <p className="fs-notice" data-tone="warning">
          No he podido leer {degraded.join(', ')}. Lo que se ve es real; eso falta.
        </p>
      )}
    </div>
  );
}
