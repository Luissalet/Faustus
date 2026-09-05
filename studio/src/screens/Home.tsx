import { ChevronRight, Code2, FileText, Image, Inbox, Search } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router';
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
import { t, tn } from '../i18n';

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

/* Each way in opens Studio with the sentence already started. */
const QUICK_STARTS = [
  { label: 'Create an image', icon: Image, draft: 'Generate an image of ' },
  { label: 'Write', icon: FileText, draft: 'Write ' },
  { label: 'Code', icon: Code2, draft: 'Code ' },
  { label: 'Research', icon: Search, draft: 'Research on the web ' },
];

export function HomeScreen() {
  const spotlight = useSpotlight();
  const navigate = useNavigate();
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
        title={t('Could not read anything from the server')}
        body={t('The new interface is alive but cannot reach the API. The previous interface keeps working and does not depend on this.')}
        primaryAction={{
          label: t('Open the previous interface'),
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
        <Skeleton label={t('Loading your home')} height="34px" width="60%" />
        <Skeleton label={t('Loading recent work')} count={4} height="44px" />
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
          {t('What do you want to')} <em>{t('finish')}</em>?
        </h1>
        <p className="fs-home__sub">
          {data.approvals.length > 0
            ? tn(data.approvals.length, '{n} thing is waiting for your decision.', '{n} things are waiting for your decision.')
            : t('Nothing blocked waiting for you. Pick up where you left off or start something.')}
        </p>
      </header>

      {data.approvals.length > 0 && (
        <Block title={t('Needs your decision')} index={1}>
          <div className="fs-list fs-list--rail">
            {data.approvals.map((approval, index) => (
              <Link
                key={approval.approval_id ?? approval.id ?? index}
                className="fs-row"
                to={`/activity?status=accion&run=approval-${encodeURIComponent(approval.approval_id ?? approval.id ?? '')}`}
                data-testid="home-approval"
              >
                <span className="fs-row__main">
                  <span className="fs-row__name">
                    {(approval.plan?.action ?? approval.action ?? approval.tool ?? t('Action awaiting approval')).replace(/_/g, ' ')}
                  </span>
                  <span className="fs-row__meta">
                    {relativeTime(approval.requested_at) || t('waiting')}
                  </span>
                </span>
                <StatusBadge status="waiting" />
              </Link>
            ))}
          </div>
        </Block>
      )}

      {sessions.length > 0 && (
        <Block title={t('Continue')} index={2}>
          <div className="fs-list fs-list--rail">
            {sessions.map((session) => (
              <Link
                key={session.id}
                className="fs-row"
                to={`/studio?s=${encodeURIComponent(session.id)}`}
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
                {session.mode === 'agent' && <StatusBadge status="succeeded" label={t('Agent')} />}
                <ChevronRight size={16} aria-hidden="true" className="fs-row__go" />
              </Link>
            ))}
          </div>
        </Block>
      )}

      {projects.length > 0 && (
        <Block title={t('Projects')} index={3}>
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

      <Block title={t('Start something')} index={4}>
        <div className="fs-quickstarts">
          {QUICK_STARTS.map((quick) => (
            <button
              key={quick.label}
              type="button"
              className="fs-tile fs-spot"
              onMouseMove={spotlight}
              data-testid={`quickstart-${quick.label.toLowerCase().replace(/\s+/g, '-')}`}
              onClick={() => navigate(`/studio?draft=${encodeURIComponent(t(quick.draft))}`)}
            >
              <span className="fs-tile__icon">
                <quick.icon size={18} aria-hidden="true" />
              </span>
              <span>{t(quick.label)}</span>
            </button>
          ))}
        </div>
      </Block>

      {!hasAnything && (
        <EmptyState
          icon={Inbox}
          title={t('Nothing to continue yet')}
          body={t('When you start a piece of work it will appear here, with its project and what was left pending.')}
          primaryAction={{
            label: t('Start in Studio'),
            onClick: () => navigate('/studio'),
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
