import {
  ArrowUpRight,
  ChevronRight,
  Code2,
  FileText,
  Image,
  Inbox,
  Search,
} from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { EmptyState, Skeleton, StatusBadge } from '../components';
import {
  byRecency,
  loadHome,
  relativeTime,
  type HomeData,
} from '../adapters/home';
import { BrandMark } from '../shell/BrandMark';
import { useSpotlight } from '../shell/useSpotlight';
import './home.css';

/**
 * Inicio (UI-030).
 *
 * The question is "what do you want to finish", not "here are your metrics".
 * Order is deliberate: what is blocked on you, then what you can continue,
 * then where you work, then how to start something new. Model, temperature
 * and GPU are absent — they are settings, not the point of the screen.
 */

function Block({
  title,
  index,
  aside,
  children,
}: {
  title: string;
  /** Position in the entrance choreography: blocks rise in this order. */
  index: number;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="fs-block fs-enter" style={{ ['--i' as string]: index }}>
      <div className="fs-block__head">
        <h2 className="fs-block__title">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

const QUICK_STARTS = [
  { label: 'Crear imagen', icon: Image },
  { label: 'Escribir', icon: FileText },
  { label: 'Programar', icon: Code2 },
  { label: 'Investigar', icon: Search },
];

export function HomeScreen() {
  const spotlight = useSpotlight();
  const [data, setData] = useState<HomeData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    loadHome(controller.signal)
      .then(setData)
      .catch(() => setFailed(true));
    return () => controller.abort();
  }, []);

  if (failed) {
    return (
      <EmptyState
        icon={Inbox}
        title="No he podido leer nada del servidor"
        body="La interfaz nueva está viva pero no alcanza la API. La interfaz anterior sigue funcionando y no depende de esto."
        primaryAction={{
          label: 'Abrir la interfaz anterior',
          onClick: () => {
            window.location.href = '/?shell=legacy';
          },
        }}
      />
    );
  }

  if (!data) {
    return (
      <div className="fs-home">
        <Skeleton label="Cargando tu inicio" height="34px" width="60%" />
        <Skeleton label="Cargando trabajos recientes" count={4} height="44px" />
      </div>
    );
  }

  const sessions = byRecency(data.sessions).slice(0, 3);
  const projects = byRecency(data.projects).slice(0, 4);
  const hasAnything = sessions.length > 0 || projects.length > 0;

  return (
    <div className="fs-home" data-testid="home">
      <header className="fs-home__head fs-enter" style={{ ['--i' as string]: 0 }}>
        <span className="fs-watermark" aria-hidden="true">
          <BrandMark size={320} />
        </span>
        <h1 className="fs-home__title">
          ¿Qué quieres <em>terminar</em>?
        </h1>
        <p className="fs-home__sub">
          {data.approvals.length > 0
            ? `${data.approvals.length} cosa${data.approvals.length === 1 ? '' : 's'} esperan una decisión tuya.`
            : 'Nada bloqueado esperándote. Continúa donde lo dejaste o empieza algo.'}
        </p>
      </header>

      {data.approvals.length > 0 && (
        <Block title="Requiere tu decisión" index={1}>
          <div className="fs-list fs-list--rail">
            {data.approvals.map((approval, index) => (
              <a
                key={approval.approval_id ?? approval.id ?? index}
                className="fs-row"
                href="/?shell=legacy"
                data-testid="home-approval"
              >
                <span className="fs-row__main">
                  <span className="fs-row__name">
                    {approval.action ?? approval.tool ?? 'Acción pendiente de aprobación'}
                  </span>
                  <span className="fs-row__meta">
                    {relativeTime(approval.requested_at) || 'esperando'}
                  </span>
                </span>
                <StatusBadge status="waiting" />
              </a>
            ))}
          </div>
        </Block>
      )}

      {sessions.length > 0 && (
        <Block title="Continuar" index={2}>
          <div className="fs-list fs-list--rail">
            {sessions.map((session) => (
              <a
                key={session.id}
                className="fs-row"
                href={`/?shell=legacy#${session.id}`}
                data-testid="home-session"
              >
                <span className="fs-row__main">
                  <span className="fs-row__name">{session.name}</span>
                  <span className="fs-row__meta">
                    {[
                      relativeTime(session.last_message_at ?? session.updated_at),
                      session.message_count ? `${session.message_count} mensajes` : null,
                      session.model,
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </span>
                {session.mode === 'agent' && <StatusBadge status="succeeded" label="Agente" />}
                {/* Continuing a conversation still happens in the legacy chat,
                    so the row says it leaves rather than surprising you with
                    a different interface after the click. */}
                <ArrowUpRight size={15} aria-hidden="true" className="fs-row__leaves" />
              </a>
            ))}
          </div>
        </Block>
      )}

      {projects.length > 0 && (
        <Block title="Proyectos" index={3}>
          <div className="fs-list fs-list--rail">
            {projects.map((project) => (
              <a
                key={project.id}
                className="fs-row"
                href={`/projects/${project.id}`}
                data-testid="home-project"
              >
                <span className="fs-row__main">
                  <span className="fs-row__name">{project.name}</span>
                  <span className="fs-row__meta">
                    {[project.workspace, relativeTime(project.updated_at)].filter(Boolean).join(' · ')}
                  </span>
                </span>
                {/* A chevron says "this goes somewhere". A folder icon on a
                    row already labelled PROYECTOS only repeats the heading. */}
                <ChevronRight size={16} aria-hidden="true" className="fs-row__go" />
              </a>
            ))}
          </div>
        </Block>
      )}

      <Block title="Empezar algo" index={4}>
        <div className="fs-quickstarts">
          {QUICK_STARTS.map((quick) => (
            <button
              key={quick.label}
              type="button"
              className="fs-tile fs-spot"
              onMouseMove={spotlight}
              data-testid={`quickstart-${quick.label.toLowerCase().replace(/\s+/g, '-')}`}
              onClick={() => {
                // Studio is UI-032/UI-033. Until then the honest thing is to
                // hand the intent to the interface that can actually run it.
                window.location.href = '/?shell=legacy';
              }}
            >
              <span className="fs-tile__icon">
                <quick.icon size={18} aria-hidden="true" />
              </span>
              <span>{quick.label}</span>
            </button>
          ))}
        </div>
      </Block>

      {!hasAnything && (
        <EmptyState
          icon={Inbox}
          title="Todavía no hay nada que continuar"
          body="Cuando empieces un trabajo aparecerá aquí, con su proyecto y lo que quedó pendiente."
          primaryAction={{
            label: 'Empezar en la interfaz anterior',
            onClick: () => {
              window.location.href = '/?shell=legacy';
            },
          }}
        />
      )}

      {data.degraded.length > 0 && (
        <p className="fs-notice" data-tone="warning" data-testid="home-degraded">
          No he podido leer {data.degraded.join(', ')}. Lo demás de esta pantalla es
          real; eso concreto falta, y prefiero decirlo a enseñarte un cero.
        </p>
      )}
    </div>
  );
}
