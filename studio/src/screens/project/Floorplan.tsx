import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { Download, ExternalLink, FolderOpen, ListChecks, Map as MapIcon } from 'lucide-react';
import { Button } from '../../components';
import { t, tn } from '../../i18n';
import {
  floorplanBlueprint, floorplanExportUrl, generateFloorplan, openFloorplan, studioFileHref,
  type Floorplan, type FloorplanFile, type FloorplanPart,
} from '../../adapters/floorplan';
import './floorplan.css';

/** Plain-language floorplan of the project's work folder (code graph parts,
 *  files, routes and outgoing requests), its HTML export and the blueprint
 *  check of a goal text. Lives on the project overview. */
export function ProjectFloorplan({ workspace }: { workspace: string }) {
  const navigate = useNavigate();
  const [plan, setPlan] = useState<Floorplan | null>(null);
  const [busy, setBusy] = useState<'' | 'generate' | 'open' | 'blueprint'>('');
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [goal, setGoal] = useState('');
  const [useModel, setUseModel] = useState(true);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    openFloorplan(workspace, controller.signal)
      .then((p) => { setPlan(p); setChecked(true); })
      .catch(() => setChecked(true));
    return () => controller.abort();
  }, [workspace]);

  const run = async (kind: 'generate' | 'open' | 'blueprint') => {
    setBusy(kind); setError('');
    try {
      if (kind === 'generate') setPlan(await generateFloorplan(workspace, useModel ? 'model' : 'off'));
      else if (kind === 'open') {
        const p = await openFloorplan(workspace);
        if (!p) setError(t('No floorplan yet for this folder. Generate one first.'));
        setPlan(p);
      } else {
        const bp = await floorplanBlueprint(workspace, goal, useModel ? 'model' : 'off');
        setPlan((p) => (p ? { ...p, blueprint: bp } : p));
        if (!plan) setPlan(await openFloorplan(workspace));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy('');
    }
  };

  const q = query.trim().toLowerCase();
  const fileHit = (f: FloorplanFile) => !q || `${f.path} ${f.label} ${f.symbols.join(' ')} ${f.routes.map((r) => `${r.method} ${r.path}`).join(' ')} ${f.requests.map((r) => r.url).join(' ')}`.toLowerCase().includes(q);
  const parts = useMemo(() => (plan?.parts ?? []).map((p: FloorplanPart) => {
    const own = !q || `${p.name} ${p.label} ${p.parent_name}`.toLowerCase().includes(q);
    return { part: p, files: own ? p.files : p.files.filter(fileHit) };
  }).filter((x) => x.files.length > 0), [plan, q]); // eslint-disable-line react-hooks/exhaustive-deps

  const openFile = (path: string) => navigate(studioFileHref(workspace, path));
  const s = plan?.stats;

  return (
    <section className="fs-panel fs-pj__card fs-floorplan" data-testid="project-floorplan">
      <div className="fs-pj__card-head">
        <h3>{t('Floorplan')}</h3>
        <div className="fs-floorplan__actions">
          <Button variant="primary" size="sm" icon={MapIcon} label={plan ? t('Regenerate') : t('Generate')} loading={busy === 'generate'} disabled={!!busy} onClick={() => void run('generate')} testId="floorplan-generate" />
          <Button variant="ghost" size="sm" icon={FolderOpen} label={t('Open')} loading={busy === 'open'} disabled={!!busy} onClick={() => void run('open')} testId="floorplan-open" />
          <Button variant="ghost" size="sm" icon={ExternalLink} label={t('Open map')} disabled={!plan} onClick={() => window.open(floorplanExportUrl(workspace, false), '_blank', 'noopener')} testId="floorplan-map" />
          <Button variant="ghost" size="sm" icon={Download} label={t('Export')} disabled={!plan} onClick={() => { window.location.href = floorplanExportUrl(workspace, true); }} testId="floorplan-export" />
        </div>
      </div>
      <p className="fs-pj__muted">{t('Every part and file of the work folder in plain words, with the routes it serves and the requests it makes.')}</p>
      <label className="fs-floorplan__check">
        <input type="checkbox" checked={useModel} onChange={(e) => setUseModel(e.target.checked)} data-testid="floorplan-use-model" />
        <span>{t('Describe files with the utility model (only changed files are described again)')}</span>
      </label>
      {error && <p role="alert" className="fs-floorplan__error">{error}</p>}
      {!plan && checked && !error && <p className="fs-pj__muted">{t('No floorplan yet for this folder.')}</p>}
      {plan && s && (
        <>
          <p className="fs-floorplan__stats" data-testid="floorplan-stats">
            {t('{parts} parts · {files} files · {routes} routes · {requests} outgoing requests', { parts: String(s.parts), files: String(s.files), routes: String(s.routes), requests: String(s.requests) })}
            {' · '}{t('{n} described now, {m} reused, {p} waiting for the model', { n: String(s.labelled_now), m: String(s.reused_labels), p: String(s.pending_labels) })}
            {plan.label_note ? ` · ${plan.label_note}` : ''}
          </p>
          <input className="fs-field fs-floorplan__search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t('Search parts, files, routes, requests')} aria-label={t('Search the floorplan')} data-testid="floorplan-search" />
          <ul className="fs-floorplan__parts">
            {parts.map(({ part, files }) => {
              const isOpen = !!q || !!expanded[part.id];
              return (
                <li key={part.id} className="fs-floorplan__part">
                  <button type="button" className="fs-floorplan__part-head" aria-expanded={isOpen} onClick={() => setExpanded((x) => ({ ...x, [part.id]: !x[part.id] }))} data-testid="floorplan-part">
                    <strong>{part.name}</strong>
                    <span className="fs-floorplan__label">{part.label}</span>
                    <small>{tn(part.files.length, '{n} file', '{n} files')}{part.parent_name ? ` · ${part.parent_name}` : ''}</small>
                  </button>
                  {isOpen && (
                    <ul className="fs-floorplan__files">
                      {files.map((f) => (
                        <li key={f.path}>
                          <button type="button" className="fs-floorplan__path" onClick={() => openFile(f.path)} title={t('Open in Studio')} data-testid="floorplan-file">{f.path}</button>
                          <span>{f.label}</span>
                          {(f.routes.length > 0 || f.requests.length > 0) && (
                            <span className="fs-floorplan__tags">
                              {f.routes.map((r) => <code key={`r${r.line}${r.path}`} data-kind="route"><b>{r.method}</b> {r.path}</code>)}
                              {f.requests.map((r) => <code key={`q${r.line}${r.url}`} data-kind="request">{'→'} <b>{r.method}</b> {r.url}</code>)}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
            {parts.length === 0 && <li className="fs-pj__muted">{t('Nothing matches.')}</li>}
          </ul>
        </>
      )}
      <div className="fs-floorplan__blueprint">
        <h4>{t('Blueprint')}</h4>
        <p className="fs-pj__muted">{t('Paste a PRD or goal; each planned part is marked as built, partly built or missing.')}</p>
        <textarea className="fs-field" rows={4} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder={t('- Orders API\n- Email notifications\n- Admin dashboard')} data-testid="floorplan-goal" />
        <Button variant="secondary" size="sm" icon={ListChecks} label={t('Check against the code')} loading={busy === 'blueprint'} disabled={!!busy || !goal.trim()} onClick={() => void run('blueprint')} testId="floorplan-blueprint" />
        {plan?.blueprint && (
          <ul className="fs-floorplan__bp" data-testid="floorplan-blueprint-result">
            {plan.blueprint.parts.map((b, i) => (
              <li key={`${b.name}${i}`}>
                <span className="fs-floorplan__status" data-status={b.status}>{t(b.status === 'exists' ? 'Built' : b.status === 'partial' ? 'Partly built' : 'Missing')}</span>
                <span><strong>{b.name}</strong>{b.description && b.description !== b.name ? ` — ${b.description}` : ''}{b.why ? <em className="fs-floorplan__why"> {b.why}</em> : null}</span>
                {b.matches[0] && (
                  <small>
                    {b.matches[0].part}
                    {b.matches[0].files[0] && <> · <button type="button" className="fs-floorplan__path" onClick={() => openFile(b.matches[0].files[0])}>{b.matches[0].files[0]}</button></>}
                  </small>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
