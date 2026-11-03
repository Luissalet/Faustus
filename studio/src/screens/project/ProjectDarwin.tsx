import { useEffect, useState } from 'react';
import { Button, Skeleton } from '../../components';
import { getJson } from '../../adapters/api';
import { listConnectors, type Connector } from '../../adapters/connectors';
import type { Project } from '../../adapters/projects';
import { useProjectSpace } from './ProjectAgora';
import { t } from '../../i18n';

/** What `GET /api/hoard/darwin/panel` returns: Darwin's `darwin_panel`, fetched by Faustus through the hub. */
export interface DarwinPanel {
  ok: boolean;
  error?: string;
  scope?: 'app' | 'family';
  app?: { app_id: string; name: string } | null;
  unresolved?: boolean;
  discoveries_total?: number;
  top?: { id: number; title: string | null; repo?: string | null; state: string; fitness?: number; fitness_reasons?: string[] }[];
  proposals_by_state?: Record<string, number>;
  gaps?: { id: number; title: string; target?: string | null }[];
  recent_proposals?: { id: number; state: string; title: string | null; source_id: number; target?: string | null }[];
  radar?: { gap_id: number; title: string; priority: number; queries: { github: string } }[];
}

const LOOPBACK = ['127.0.0.1', 'localhost', '[::1]', '::1'];

/** A link into Darwin's own page: `?app=` scopes its lists, `?open=` opens one discovery. On a loopback host the link
 *  follows the host Studio is open on. */
export function darwinLink(appUrl: string, params: { app?: string; open?: number }, host: string): string {
  const u = new URL(appUrl);
  if (LOOPBACK.includes(u.hostname) && LOOPBACK.includes(host)) u.hostname = host;
  u.pathname = '/';
  u.search = '';
  if (params.app) u.searchParams.set('app', params.app);
  if (params.open) u.searchParams.set('open', String(params.open));
  u.hash = 'history';
  return u.toString();
}

export const isDarwin = (c: Connector): boolean => (c.preset_id ?? c.preset?.id) === 'darwin' && !!c.app_url;

export function darwinPanelPath(space: string | undefined, project: string): string {
  const q = new URLSearchParams({ project });
  if (space) q.set('space', space);
  return `/api/hoard/darwin/panel?${q}`;
}

/** Darwin's Hoard on the project overview: discoveries worth a look, proposals by state, gaps and (for Research) the
 *  radar's next searches for the Hoard this project is about. Shown when a Darwin connector exists; links open Darwin. */
export function ProjectOverviewDarwin({ project }: { project: Project }) {
  const { space, loading } = useProjectSpace(project);
  const [darwin, setDarwin] = useState<Connector | null>(null);
  const [panel, setPanel] = useState<DarwinPanel | null>(null);
  useEffect(() => {
    let live = true;
    listConnectors().then((all) => { if (live) setDarwin(all.find(isDarwin) ?? null); }).catch(() => { if (live) setDarwin(null); });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    if (!darwin || loading) return;
    const controller = new AbortController();
    setPanel(null);
    getJson<DarwinPanel>(darwinPanelPath(space?.id, project.name), controller.signal).then(setPanel)
      .catch((e: Error) => { if (!controller.signal.aborted) setPanel({ ok: false, error: e.message }); });
    return () => controller.abort();
  }, [darwin, loading, space?.id, project.name]);
  if (!darwin?.app_url) return null;
  const host = window.location.hostname;
  const appId = panel?.app?.app_id;
  const link = (open?: number) => darwinLink(darwin.app_url as string, { app: appId, open }, host);
  const counts = Object.entries(panel?.proposals_by_state ?? {}).sort((a, b) => b[1] - a[1]);
  return <section className="fs-panel" data-testid="project-overview-darwin">
    <div className="fs-pj__card-head"><h2>Darwin{panel?.ok ? ` · ${panel.app ? panel.app.name : t('The whole family')}` : ''}</h2>
      <Button size="sm" variant="secondary" label={t('Open Darwin')} onClick={() => window.open(link(), '_blank', 'noopener')} /></div>
    {!panel ? <Skeleton label={t('Loading Darwin')} count={2} />
      : !panel.ok ? <p className="fs-pj__muted" role="status">{t("Darwin's Hoard is not answering: {error}", { error: panel.error ?? '' })}</p>
      : <>
        {panel.unresolved && <p className="fs-pj__muted">{t('Darwin does not recognise this project as a Hoard: showing the whole family.')}</p>}
        {counts.length > 0 && <p data-testid="darwin-proposal-counts">{counts.map(([k, n]) => `${k} ${n}`).join(' · ')}</p>}
        <h3>{t('Worth a look')}</h3>
        {panel.top?.length ? <ul className="fs-project-workspace__list">{panel.top.map((d) => <li key={d.id}><div>
          <a className="fs-link" href={link(d.id)} target="_blank" rel="noopener noreferrer">{d.title || d.repo || `#${d.id}`}</a>
          <p title={(d.fitness_reasons ?? []).join(' · ')}>{d.fitness ?? 0}/100 · {d.state}</p></div></li>)}</ul>
          : <p className="fs-pj__muted">{t('Nothing open.')}</p>}
        {!!panel.gaps?.length && <><h3>{t('Gaps')}</h3><ul className="fs-project-workspace__list">{panel.gaps.slice(0, 6).map((g) =>
          <li key={g.id}><div><strong>{g.title}</strong>{!panel.app && g.target ? <p>{g.target}</p> : null}</div></li>)}</ul></>}
        {!!panel.radar?.length && <><h3>{t('Next radar searches')}</h3><ul className="fs-project-workspace__list">{panel.radar.map((g) =>
          <li key={g.gap_id}><div><strong>{g.title}</strong><p>{g.priority} · <code>{g.queries.github}</code></p></div></li>)}</ul></>}
        {!!panel.recent_proposals?.length && <><h3>{t('Recent changes')}</h3><ul className="fs-project-workspace__list">{panel.recent_proposals.slice(0, 4).map((r) =>
          <li key={r.id}><div><a className="fs-link" href={link(r.source_id)} target="_blank" rel="noopener noreferrer">{r.title || `#${r.source_id}`}</a>
            <p>{r.state}{r.target && !panel.app ? ` · ${r.target}` : ''}</p></div></li>)}</ul></>}
      </>}
  </section>;
}
