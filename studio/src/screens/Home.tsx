import { Bot, CloudSun, Code2, Eye, FileText, GitBranch, Image, Mail, Newspaper, RefreshCw, Search } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import { Button, Skeleton, StatusBadge } from '../components';
import {
  byRecency,
  loadHome,
  relativeTime,
  type HomeData,
} from '../adapters/home';
import { describeTrigger, runAutomation, type Automation } from '../adapters/automations';
import { listHomeCards, unpinHomeCard, type HomeCard } from '../adapters/homeCards';
import { usageRecap, type UsageRecap } from '../adapters/commands';
import { RadarRows, useGitRadar } from './source-control/GitRadar';
import './source-control.css';
import { Rich } from './rich';
import './home.css';
import { FleetCard } from './FleetCard';
import { PersonalAssistants } from './home/PersonalAssistants';
import { ContextualTools } from '../shell/contextualTools';
import { t, tn } from '../i18n';

/**
 * Studio's start content reuses the former Home stores and controls.
 * Assistants and actionable results stay near the composer. Installation-wide
 * summaries are available on demand, and never mount inside an open chat.
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

/* ── Your cards: automations pinned to Home, with their latest result ── */

const CARD_ICON: Record<string, typeof Bot> = {
  weather_report: CloudSun,
  news_brief: Newspaper,
  watch_page: Eye,
  mail_digest: Mail,
  git_radar: GitBranch,
};

/** `describeTrigger` reads an Automation; a card carries the same schedule
 *  fields, so it is built as a partial one rather than duplicating the
 *  cron/weekday/monthly-day wording here. */
function cardSchedule(card: HomeCard): string {
  return describeTrigger({
    id: card.task_id,
    name: card.name,
    schedule: card.schedule,
    scheduled_time: card.scheduled_time,
    timezone: card.timezone,
    cron_expression: card.cron_expression,
  } as Automation);
}

function HomeCardTile({ card, onChanged }: { card: HomeCard; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [confirmUnpin, setConfirmUnpin] = useState(false);
  const Icon = (card.action && CARD_ICON[card.action]) || Bot;

  const refresh = async () => {
    setBusy(true);
    try {
      // Forced: a person pressed Refresh, so the run must not be parked
      // behind the "wait until Faustus is idle" gate that background runs use.
      await runAutomation(card.task_id, true);
    } catch {
      /* the next poll shows the failed run through last_status/last_error */
    } finally {
      onChanged();
      setBusy(false);
    }
  };

  const unpin = async () => {
    setConfirmUnpin(false);
    try {
      await unpinHomeCard(card.task_id);
    } finally {
      onChanged();
    }
  };

  return (
    <article className="fs-card-home" data-testid="home-card">
      <header className="fs-card-home__head">
        <span className="fs-card-home__icon">
          <Icon size={15} aria-hidden="true" />
        </span>
        <span className="fs-card-home__meta">
          <span className="fs-card-home__name">{card.name}</span>
          <span className="fs-card-home__sched">{cardSchedule(card)}</span>
        </span>
        {card.running ? (
          <span className="fs-card-home__pill" data-tone="running" data-testid="home-card-running">
            {t('Running…')}
          </span>
        ) : card.last_status === 'error' ? (
          <span className="fs-card-home__pill" data-tone="error" title={card.last_error ?? undefined}>
            {t('Failed')}
          </span>
        ) : card.last_status === 'skipped' ? (
          <span className="fs-card-home__pill" data-tone="muted">
            {t('No change')}
          </span>
        ) : null}
      </header>

      <div className="fs-card-home__body">
        {card.result ? <Rich text={card.result} /> : <p className="fs-card-home__empty">{t('No result yet — Refresh to run it now.')}</p>}
      </div>

      <footer className="fs-card-home__foot">
        <span className="fs-card-home__updated">{card.result_at ? t('updated {when}', { when: relativeTime(card.result_at) }) : ''}</span>
        <span className="fs-card-home__actions">
          {confirmUnpin ? (
            <span className="fs-modes__confirm">
              <span className="fs-set__help" data-tone="bad">
                {t('Unpin?')}
              </span>
              <Button variant="danger-solid" size="sm" label={t('Yes')} onClick={() => void unpin()} />
              <Button variant="ghost" size="sm" label={t('No')} onClick={() => setConfirmUnpin(false)} />
            </span>
          ) : (
            <>
              <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} loading={busy} disabled={card.running} onClick={() => void refresh()} testId="home-card-refresh" />
              <Link className="fs-btn" data-variant="ghost" data-size="sm" to="/automations">
                <span>{t('Open')}</span>
              </Link>
              <Button variant="ghost" size="sm" label={t('Unpin')} onClick={() => setConfirmUnpin(true)} testId="home-card-unpin" />
            </>
          )}
        </span>
      </footer>
    </article>
  );
}

function HomeCards({ cards, onChanged }: { cards: HomeCard[]; onChanged: () => void }) {
  return (
    <Block title={t('Your cards')} index={1}>
      {cards.length === 0 ? (
        <p className="fs-card-home__empty-block" data-testid="home-cards-empty">
          {t('Pin an automation to see its result here — ask in a chat («¿qué tiempo hace en X mañana? que se repita cada día») or')}{' '}
          <Link to="/automations">{t('pin one in Automations')}</Link>.
        </p>
      ) : (
        <div className="fs-cards" data-testid="home-cards">
          {cards.map((card) => (
            <HomeCardTile key={card.task_id} card={card} onChanged={onChanged} />
          ))}
        </div>
      )}
    </Block>
  );
}

/* Each way in opens Studio with the sentence already started. */
const QUICK_STARTS = [
  { label: 'Create an image', icon: Image, draft: 'Generate an image of ' },
  { label: 'Write', icon: FileText, draft: 'Write ' },
  { label: 'Code', icon: Code2, draft: 'Code ' },
  { label: 'Research', icon: Search, draft: 'Research on the web ' },
];

/* ── Needs commit or push: the git radar, live (src/git_radar.py) ──
 * Not a pinned automation: it reads the repositories directly, so it is
 * always current and needs no schedule. Absent entirely when everything
 * is committed and pushed — a green "all clear" on Home is noise. */
const RADAR_HOME_LIMIT = 6;

function GitRadarBlock() {
  const { radar } = useGitRadar();
  if (!radar || radar.attention_count === 0) return null;
  const more = radar.attention_count - RADAR_HOME_LIMIT;
  return (
    <Block
      title={t('Needs commit or push')}
      index={2}
      aside={
        <Link className="fs-block__more" to="/source-control" data-testid="home-radar-all">
          <GitBranch size={14} aria-hidden="true" />
          {more > 0 ? t('{n} more in Source control', { n: more }) : t('Open Source control')}
        </Link>
      }
    >
      <RadarRows repos={radar.attention} limit={RADAR_HOME_LIMIT} />
    </Block>
  );
}

/* ── Your month: usage across every chat (src/usage_recap.py) ──
 * One query, no model call. Absent until there is a turn with metrics; a
 * row of zeros is noise. `/recap` in Studio has the full table. */
const fmtK = (n?: number) => {
  const v = Number(n || 0);
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e4) return `${Math.round(v / 1e3)}k`;
  return String(v);
};

function UsageRecapBlock() {
  const [recap, setRecap] = useState<UsageRecap | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    usageRecap(30, controller.signal)
      .then(setRecap)
      .catch(() => {
        /* no card rather than a broken Home */
      });
    return () => controller.abort();
  }, []);
  const tot = recap?.total;
  if (!recap || !tot?.turns) return null;
  const topModel = Object.entries(recap.by_model ?? {}).sort((a, b) => (b[1].turns ?? 0) - (a[1].turns ?? 0))[0];
  const stats: { label: string; value: string; hint?: string }[] = [
    { label: t('Turns'), value: fmtK(tot.turns), hint: tn(recap.chats, 'in {n} chat', 'in {n} chats') },
    { label: t('Local'), value: fmtK(recap.turns_local), hint: t('{n} hosted', { n: recap.turns_hosted }) },
    { label: t('Tokens'), value: fmtK((tot.input_tokens ?? 0) + (tot.output_tokens ?? 0)), hint: t('{i} in · {o} out', { i: fmtK(tot.input_tokens), o: fmtK(tot.output_tokens) }) },
  ];
  if (tot.cache_hit_percent !== undefined && tot.cache_hit_percent !== null) {
    stats.push({ label: t('Prompt from cache'), value: `${tot.cache_hit_percent} %` });
  }
  if (tot.tool_calls) stats.push({ label: t('Tool calls'), value: fmtK(tot.tool_calls) });
  if (tot.cost_usd) stats.push({ label: t('Cost'), value: `$${Number(tot.cost_usd).toFixed(2)}` });
  const tools = (recap.top_tools ?? []).slice(0, 4);
  return (
    <Block
      title={t('Your last 30 days')}
      index={3}
      aside={
        <Link className="fs-block__more" to={`/studio?draft=${encodeURIComponent('/recap 30')}`} data-testid="home-recap-more">
          {t('Full recap')}
        </Link>
      }
    >
      <div className="fs-recap" data-testid="home-recap">
        {stats.map((s) => (
          <div key={s.label} className="fs-recap__stat">
            <span className="fs-recap__value">{s.value}</span>
            <span className="fs-recap__label">{s.label}</span>
            {s.hint && <span className="fs-recap__hint">{s.hint}</span>}
          </div>
        ))}
      </div>
      {(topModel || tools.length > 0) && (
        <p className="fs-recap__line">
          {topModel && (
            <>
              {t('Most used model')}: <code>{topModel[0]}</code>
            </>
          )}
          {topModel && tools.length > 0 && ' · '}
          {tools.length > 0 && `${t('Top tools')}: ${tools.map(([name, n]) => `${name} ${n}`).join(', ')}`}
        </p>
      )}
    </Block>
  );
}

/** The useful start-of-work surfaces, inside Studio's existing chat shell. */
export function StudioStart({ onDraft, welcome = true }: { onDraft: (text: string) => void; welcome?: boolean }) {
  const [data, setData] = useState<HomeData | null>(null);
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const [cards, setCards] = useState<HomeCard[]>([]);
  const [cardsError, setCardsError] = useState('');
  const [cardsRevision, setCardsRevision] = useState(0);
  const [overview, setOverview] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const load = () => void loadHome(controller.signal)
      .then(value => { if (!controller.signal.aborted) { setData(value); setFailed(false); } })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    load();
    const timer = window.setInterval(load, 60000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [revision]);

  useEffect(() => {
    let live = true;
    let timer: number | undefined;
    const load = async () => {
      let next: HomeCard[] = [];
      try {
        next = await listHomeCards();
        if (live) { setCards(next); setCardsError(''); }
      } catch (error) { if (live) setCardsError((error as Error).message); }
      if (live) timer = window.setTimeout(() => void load(), next.some(card => card.running) ? 5000 : 60000);
    };
    void load();
    return () => { live = false; window.clearTimeout(timer); };
  }, [cardsRevision]);
  const reloadCards = () => setCardsRevision(value => value + 1);
  const sessions = byRecency(data?.sessions ?? []).slice(0, 3);
  const projects = byRecency(data?.projects ?? []).slice(0, 4);

  return <div className="fs-studio-start" data-testid="studio-start">
    <ContextualTools context="personal" />
    {welcome && <div className="fs-studio-start__intro">
      <p>{t('Start a conversation below or continue with an assistant.')}</p>
      <div className="fs-studio-start__shortcuts" aria-label={t('Start something')}>
        {QUICK_STARTS.map(quick => <Button key={quick.label} size="sm" variant="ghost" icon={quick.icon} label={t(quick.label)} onClick={() => onDraft(t(quick.draft))} testId={`quickstart-${quick.label.toLowerCase().replace(/\s+/g, '-')}`} />)}
      </div>
    </div>}

    <PersonalAssistants />

    {!!data?.approvals.length && <Block title={t('Needs your decision')} index={0}>
      <div className="fs-studio-start__rows">{data.approvals.map((approval, index) => <Link className="fs-studio-start__row" key={approval.approval_id ?? approval.id ?? index}
        to={`/activity?status=accion&run=approval-${encodeURIComponent(approval.approval_id ?? approval.id ?? '')}`} data-testid="home-approval">
        <span>{(approval.plan?.action ?? approval.action ?? approval.tool ?? t('Action awaiting approval')).replace(/_/g, ' ')}</span><StatusBadge status="waiting" />
      </Link>)}</div>
    </Block>}

    {cards.length > 0 && <HomeCards cards={cards} onChanged={reloadCards} />}
    {cardsError && <p className="fs-notice" data-tone="warning" role="alert">{cardsError} <Button size="sm" variant="ghost" label={t('Retry')} onClick={reloadCards} /></p>}

    {sessions.length > 0 && <section className="fs-studio-start__recent" aria-label={t('Recent conversations')}>
      <h2>{t('Continue')}</h2><div className="fs-studio-start__rows">{sessions.map(session => <Link key={session.id} className="fs-studio-start__row" to={`/studio?s=${encodeURIComponent(session.id)}`} data-testid="home-session">
        <span>{session.name}</span><small>{relativeTime(session.last_message_at ?? session.updated_at)}</small>
      </Link>)}</div>
    </section>}

    {projects.length > 0 && <section className="fs-studio-start__projects" aria-label={t('Projects')}>
      <div className="fs-studio-start__section-head"><h2>{t('Projects')}</h2><Link to="/projects">{t('View all')}</Link></div>
      <div className="fs-studio-start__shortcuts">{projects.map(project => <Link key={project.id} to={`/projects/${encodeURIComponent(project.id)}`} className="fs-studio-start__project" data-testid="home-project">{project.name}</Link>)}</div>
    </section>}

    {!data && !failed && <Skeleton label={t('Loading recent work')} count={2} height="36px" />}
    {failed && <p className="fs-notice" data-tone="warning" role="alert">{t('Could not load recent work. You can still use the conversation.')} <Button size="sm" variant="ghost" label={t('Retry')} onClick={() => setRevision(value => value + 1)} /></p>}
    {!!data?.degraded.length && <p className="fs-notice" data-tone="warning" data-testid="home-degraded">{t('Some start information could not be loaded: {sources}.', {sources: data.degraded.join(', ')})} <Button size="sm" variant="ghost" label={t('Retry')} onClick={() => setRevision(value => value + 1)} /></p>}

    <details className="fs-studio-start__overview" onToggle={event => setOverview(event.currentTarget.open)}>
      <summary>{t('Workspace overview')}</summary>
      {overview && <div className="fs-studio-start__overview-content"><GitRadarBlock /><FleetCard /><UsageRecapBlock /></div>}
    </details>
  </div>;
}
