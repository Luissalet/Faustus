import { ChevronRight, FolderKanban, Search } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { EmptyState, Skeleton } from '../components';
import { listProjects, type Project } from '../adapters/projects';
import { relativeTime } from '../adapters/home';
import './projects.css';
import './home.css';

/**
 * Proyectos (UI-040).
 *
 * The list used to live inside `#projects-modal`: no URL, no browser back,
 * nothing to bookmark or send to yourself. It is a route now, and the filter
 * lives in the query string, so a filtered view is a link.
 */
export function ProjectsScreen() {
  const [params, setParams] = useSearchParams();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [failed, setFailed] = useState(false);
  const query = params.get('q') ?? '';

  useEffect(() => {
    const controller = new AbortController();
    listProjects(controller.signal).then(setProjects).catch(() => setFailed(true));
    return () => controller.abort();
  }, []);

  const visible = useMemo(() => {
    if (!projects) return [];
    const needle = query.trim().toLowerCase();
    if (!needle) return projects;
    return projects.filter((project) =>
      [project.name, project.workspace, project.folder]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(needle)),
    );
  }, [projects, query]);

  if (failed) {
    return (
      <EmptyState
        icon={FolderKanban}
        title="No he podido leer tus proyectos"
        body="La API no responde. La interfaz anterior no depende de esta pantalla y sigue funcionando."
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
    <div className="fs-screen" data-testid="projects">
      <header className="fs-screen__head">
        <h1 className="fs-screen__title">Proyectos</h1>
        <label className="fs-search">
          <Search size={15} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder="Filtrar por nombre o carpeta"
            aria-label="Filtrar proyectos"
            data-testid="projects-filter"
            onChange={(event) => {
              const next = new URLSearchParams(params);
              if (event.target.value) next.set('q', event.target.value);
              else next.delete('q');
              // replace, not push: typing must not fill the back button with
              // one history entry per keystroke.
              setParams(next, { replace: true });
            }}
          />
        </label>
      </header>

      {!projects && <Skeleton label="Cargando proyectos" count={4} height="44px" />}

      {projects && visible.length === 0 && (
        <EmptyState
          icon={FolderKanban}
          title={query ? 'Ningún proyecto coincide' : 'Todavía no hay proyectos'}
          body={
            query
              ? 'Prueba con otra parte del nombre o de la ruta de la carpeta.'
              : 'Un proyecto agrupa carpeta, instrucciones, memoria y conversaciones. Crear el primero sigue siendo cosa de la interfaz anterior por ahora.'
          }
          primaryAction={
            query
              ? undefined
              : {
                  label: 'Crear en la interfaz anterior',
                  onClick: () => {
                    window.location.href = '/?shell=legacy';
                  },
                }
          }
        />
      )}

      {projects && visible.length > 0 && (
        <div className="fs-list">
          {visible.map((project) => (
            <Link
              key={project.id}
              to={`/projects/${project.id}`}
              className="fs-row"
              data-testid="project-row"
            >
              <span className="fs-row__main">
                <span className="fs-row__name">{project.name}</span>
                <span className="fs-row__meta">
                  {[project.workspace, relativeTime(project.updated_at)]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </span>
              <ChevronRight size={16} aria-hidden="true" className="fs-row__go" />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
