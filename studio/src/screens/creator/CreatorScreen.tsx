import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { RefreshCw, FileWarning, Sparkles } from 'lucide-react';
import { Button, EmptyState, Skeleton, Toast } from '../../components';
import { listProjects, type Project } from '../../adapters/projects';
import * as api from '../../adapters/creator';
import { t } from '../../i18n';
import { Timeline } from './Timeline';
import './creator.css';

/**
 * WP05 — Creator shell.
 *
 * A three-zone workspace over the WP02/WP03/WP07/WP09/WP30 Creator API:
 * project library (left), the active document with its command history and
 * a minimal command editor (center), and capabilities/resources/preflight
 * (right). No second chat loop lives here — every action is a typed call
 * through `adapters/creator.ts` onto the API that already exists.
 *
 * Gated end to end on `creator_enabled` (CONTRATO.md rule 5): the sidebar
 * entry only appears once `GET /api/creator/capabilities` answers something
 * other than 404 (see `shell/routes.ts`'s `useCreatorAvailable`), and this
 * screen re-checks the same flag on its own mount so a direct link or a
 * stale nav still lands on the disabled message rather than a broken UI.
 */

/**
 * Minimal, structurally-valid content for each document kind (mirrors
 * `src/creator/documents.py`'s own `_validate_*` requirements) so "new
 * document" actually creates something the server accepts, instead of the
 * empty `{}` `validate_content` rejects with a 400 for every real kind.
 */
function defaultContentFor(kind: string): Record<string, unknown> {
  switch (kind) {
    case 'canvas':
      return { width: 1024, height: 1024, layers: [], base_asset_ref: 'none' };
    case 'timeline':
      return {
        clock: { ticks_per_second_numerator: '30', ticks_per_second_denominator: '1' },
        duration_ticks: '0',
        tracks: [{ id: 'track-1', kind: 'video', locked: false, clips: [] }],
      };
    case 'transcript':
      return { source_asset_ref: 'none', sample_rate: 48000, cues: [], alignment_status: 'not_aligned' };
    case 'song':
      return { language: 'en', sections: [{ id: 'sec-1', kind: 'verse', lyrics: '' }], takes: [], selected_take: null };
    case 'storyboard':
      return {
        shots: [{
          id: 'shot-1', brief: '', duration: { start_ticks: '0', duration_ticks: '1' },
          clock: { ticks_per_second_numerator: '30', ticks_per_second_denominator: '1' },
          reference_assets: [], selected_take: null,
        }],
      };
    default:
      return {};
  }
}

const DRAFT_PREFIX = 'faustus_creator_draft_';

interface Draft {
  docId: string | null;
  panelWidths: { left: number; right: number };
  libraryFilter: { kind: string; q: string };
}

function readDraft(projectId: string): Draft {
  const fallback: Draft = { docId: null, panelWidths: { left: 280, right: 320 }, libraryFilter: { kind: '', q: '' } };
  try {
    const raw = window.localStorage.getItem(DRAFT_PREFIX + projectId);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<Draft>;
    return {
      docId: parsed.docId ?? null,
      panelWidths: parsed.panelWidths ?? fallback.panelWidths,
      libraryFilter: parsed.libraryFilter ?? fallback.libraryFilter,
    };
  } catch {
    return fallback;
  }
}

function writeDraft(projectId: string, draft: Draft): void {
  try {
    window.localStorage.setItem(DRAFT_PREFIX + projectId, JSON.stringify(draft));
  } catch {
    /* private mode, quota — the draft is a convenience, never load-bearing */
  }
}

function bytesLabel(n: number | null | undefined): string {
  if (!n && n !== 0) return '—';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

// ── top bar: project selector ───────────────────────────────────────────

function ProjectSelector({ projects, value, onChange }: { projects: Project[]; value: string; onChange: (id: string) => void }) {
  return (
    <label className="fs-creator__project-select">
      <span>{t('Project')}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={t('Project')}
        data-testid="creator-project-select"
      >
        <option value="">{t('Select a project…')}</option>
        {projects.map((p) => (
          <option key={p.id} value={p.id}>{p.name}</option>
        ))}
      </select>
    </label>
  );
}

// ── left zone: library ──────────────────────────────────────────────────

function LibraryPanel({
  projectId, filter, onFilterChange, activeDocId, onOpen, items, loading, error,
}: {
  projectId: string;
  filter: { kind: string; q: string };
  onFilterChange: (f: { kind: string; q: string }) => void;
  activeDocId: string | null;
  onOpen: (item: api.LibraryItem) => void;
  items: api.LibraryItem[];
  loading: boolean;
  error: string | null;
}) {
  return (
    <section className="fs-creator__library" aria-label={t('Project library')}>
      <div className="fs-creator__library-filters">
        <select
          value={filter.kind}
          onChange={(e) => onFilterChange({ ...filter, kind: e.target.value })}
          aria-label={t('Filter by kind')}
        >
          <option value="">{t('All kinds')}</option>
          {['image', 'video', 'audio', 'document', 'canvas', 'timeline', 'transcript', 'song', 'storyboard'].map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
        <input
          type="search"
          placeholder={t('Search…')}
          value={filter.q}
          onChange={(e) => onFilterChange({ ...filter, q: e.target.value })}
          aria-label={t('Search the library')}
        />
      </div>
      {loading && <Skeleton label={t('Loading the library')} count={4} height="52px" />}
      {!loading && error && (
        <EmptyState icon={FileWarning} title={t('Could not load the library')} body={error} />
      )}
      {!loading && !error && items.length === 0 && (
        <EmptyState title={t('Nothing here yet')} body={t('Occurrences produced in this project will show up here.')} />
      )}
      {!loading && !error && items.length > 0 && (
        <ul className="fs-creator__library-list" role="listbox" aria-label={t('Occurrences')}>
          {items.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                className="fs-creator__library-item"
                role="option"
                aria-selected={activeDocId === item.id}
                onClick={() => onOpen(item)}
                data-testid={`creator-lib-${item.id}`}
              >
                {item.media_type && item.media_type.startsWith('image/') ? (
                  <img className="fs-creator__thumb" src={item.download_url} alt="" />
                ) : (
                  <span className="fs-creator__thumb fs-creator__thumb--placeholder" aria-hidden="true">{item.kind.slice(0, 2).toUpperCase()}</span>
                )}
                <span className="fs-creator__library-meta">
                  <b>{item.label}</b>
                  <span className="fs-creator__muted">{item.kind} · {bytesLabel(item.byte_size)}</span>
                  {item.partial && <span className="fs-creator__tag">{t('partial')}</span>}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ── center zone: active document ────────────────────────────────────────

function DocumentPanel({
  doc, history, loading, error, onCreate, onPatch, onReload, projectId, kinds, onDocUpdated,
}: {
  doc: api.CreatorDocument | null;
  history: api.CommandHistoryEntry[];
  loading: boolean;
  error: string | null;
  onCreate: (kind: string) => void;
  onPatch: (commandId: string, expectedRevision: number, patchJson: string) => Promise<void>;
  onReload: () => void;
  projectId: string;
  kinds: string[];
  onDocUpdated: (doc: api.CreatorDocument) => void;
}) {
  const [view, setView] = useState<'timeline' | 'json'>('timeline');
  const [patchText, setPatchText] = useState('{}');
  const [commandId, setCommandId] = useState('');
  const [expectedRevision, setExpectedRevision] = useState<number | ''>('');
  const [conflict, setConflict] = useState<{ current: number } | null>(null);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  useEffect(() => {
    if (doc) {
      setExpectedRevision(doc.revision);
      setConflict(null);
    }
  }, [doc?.id, doc?.revision]);

  const submit = useCallback(async () => {
    if (!doc || expectedRevision === '') return;
    setSending(true);
    setSendError(null);
    setConflict(null);
    try {
      const patch = JSON.parse(patchText || '{}');
      const cid = commandId.trim() || `cmd-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      await onPatch(cid, Number(expectedRevision), JSON.stringify(patch));
      setCommandId('');
    } catch (err) {
      if (err instanceof api.RevisionConflictError) {
        setConflict({ current: err.currentRevision });
      } else if (err instanceof SyntaxError) {
        setSendError(t('The patch must be valid JSON.'));
      } else {
        setSendError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSending(false);
    }
  }, [doc, expectedRevision, patchText, commandId, onPatch]);

  if (!projectId) {
    return <EmptyState icon={Sparkles} title={t('Choose a project')} body={t('Pick a project above to open its Creator documents.')} />;
  }

  if (loading) return <Skeleton label={t('Loading the document')} count={6} height="20px" />;

  if (!doc) {
    return (
      <div className="fs-creator__doc-empty">
        <EmptyState
          title={t('No document open')}
          body={t('Open one from the library on the left, or start a new one.')}
        />
        <div className="fs-creator__new-doc">
          <span>{t('New document:')}</span>
          {kinds.map((k) => (
            <Button key={k} size="sm" variant="ghost" label={k} onClick={() => onCreate(k)} testId={`creator-new-${k}`} />
          ))}
        </div>
      </div>
    );
  }

  return (
    <section className="fs-creator__document" aria-label={t('Active document')}>
      <header className="fs-creator__doc-head">
        <div>
          <b>{doc.kind}</b>
          <span className="fs-creator__muted"> · {t('revision {n}', { n: doc.revision })} · {doc.state}</span>
        </div>
        <Button size="sm" variant="ghost" icon={RefreshCw} label={t('Reload')} onClick={onReload} testId="creator-doc-reload" />
      </header>

      {error && <p className="fs-creator__error" role="alert">{error}</p>}

      {doc.kind === 'timeline' && (
        <div className="fs-creator__view-tabs" role="tablist" aria-label={t('Document view')}>
          <button type="button" role="tab" aria-selected={view === 'timeline'}
            className={view === 'timeline' ? 'fs-creator__view-tab fs-creator__view-tab--active' : 'fs-creator__view-tab'}
            onClick={() => setView('timeline')} data-testid="creator-view-timeline">
            {t('Timeline')}
          </button>
          <button type="button" role="tab" aria-selected={view === 'json'}
            className={view === 'json' ? 'fs-creator__view-tab fs-creator__view-tab--active' : 'fs-creator__view-tab'}
            onClick={() => setView('json')} data-testid="creator-view-json">
            {t('JSON')}
          </button>
        </div>
      )}

      {doc.kind === 'timeline' && view === 'timeline' ? (
        <Timeline doc={doc} onDocUpdated={onDocUpdated} onRevisionConflict={onReload} />
      ) : (
        <pre className="fs-creator__json" tabIndex={0} aria-label={t('Document content (JSON)')}>
          {JSON.stringify(doc.content, null, 2)}
        </pre>
      )}

      <details className="fs-creator__history">
        <summary>{t('Command history')} ({history.length})</summary>
        <ul>
          {history.map((h, i) => (
            <li key={`${h.command_id}-${i}`}>
              <code>{h.command_id}</code>{' '}
              <span className={h.applied ? 'fs-creator__ok' : 'fs-creator__muted'}>
                {h.applied ? t('applied') : t('deduped')}
              </span>
            </li>
          ))}
          {history.length === 0 && <li className="fs-creator__muted">{t('No commands yet.')}</li>}
        </ul>
      </details>

      <form
        className="fs-creator__command-editor"
        onSubmit={(e) => { e.preventDefault(); void submit(); }}
        aria-label={t('Apply a command')}
      >
        <h4>{t('Command: patch_content')}</h4>
        <label>
          {t('expected_revision')}
          <input
            type="number"
            value={expectedRevision}
            onChange={(e) => setExpectedRevision(e.target.value === '' ? '' : Number(e.target.value))}
            aria-label={t('expected_revision')}
          />
        </label>
        <label>
          {t('command_id (optional, dedupe key)')}
          <input
            type="text"
            value={commandId}
            onChange={(e) => setCommandId(e.target.value)}
            aria-label={t('command_id')}
          />
        </label>
        <label>
          {t('patch (JSON, merged into content)')}
          <textarea
            value={patchText}
            onChange={(e) => setPatchText(e.target.value)}
            rows={5}
            aria-label={t('patch JSON')}
            data-testid="creator-patch-input"
          />
        </label>
        {conflict && (
          <div className="fs-creator__conflict" role="alert" data-testid="creator-conflict">
            <p>{t('This document changed since you loaded it. The server now has revision {n}.', { n: conflict.current })}</p>
            <Button size="sm" variant="secondary" label={t('Reload and try again')} onClick={onReload} testId="creator-conflict-reload" />
          </div>
        )}
        {sendError && <p className="fs-creator__error" role="alert">{sendError}</p>}
        <Button type="submit" size="sm" variant="primary" label={t('Send command')} loading={sending} disabled={sending || expectedRevision === ''} testId="creator-send-command" />
      </form>
    </section>
  );
}

// ── right zone: capabilities / resources / preflight ────────────────────

function ResourcesCard({ resources }: { resources: api.ResourcesSnapshot | null }) {
  if (!resources) return <Skeleton label={t('Loading resources')} count={3} height="16px" />;
  const gpus = Array.isArray(resources.inventory.gpus) ? resources.inventory.gpus : [];
  return (
    <div className="fs-creator__card">
      <h4>{t('Devices')}</h4>
      <p className="fs-creator__muted">{t('Mode: {mode}', { mode: resources.inventory.mode })}</p>
      {gpus.length === 0 && <p className="fs-creator__muted">{t('No GPU detected.')}</p>}
      <ul>
        {gpus.map((g, i) => {
          const gpu = g as Record<string, unknown>;
          return (
            <li key={i}>
              {String(gpu.name ?? `GPU ${i}`)} — {bytesLabel(Number(gpu.vram_free_bytes ?? gpu.free_bytes ?? 0))} {t('free')}
            </li>
          );
        })}
      </ul>
      <p className="fs-creator__muted">
        {t('{n} active admission(s), {q} queued', { n: resources.active_admissions.length, q: resources.queue.length })}
      </p>
    </div>
  );
}

function DeploymentsCard({ deployments }: { deployments: api.DeploymentSummary[] | null }) {
  if (!deployments) return <Skeleton label={t('Loading deployments')} count={2} height="16px" />;
  if (deployments.length === 0) return <p className="fs-creator__muted">{t('No known deployments yet.')}</p>;
  return (
    <div className="fs-creator__card">
      <h4>{t('Known deployments')}</h4>
      <ul>
        {deployments.map((d) => (
          <li key={d.deployment_id}>
            <code>{d.deployment_id}</code>
          </li>
        ))}
      </ul>
    </div>
  );
}

function PreflightCard({ projectId, deployments }: { projectId: string; deployments: api.DeploymentSummary[] | null }) {
  const [operation, setOperation] = useState('');
  const [engine, setEngine] = useState('');
  const [deploymentId, setDeploymentId] = useState('');
  const [paramsText, setParamsText] = useState('{}');
  const [report, setReport] = useState<api.PreflightReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [approved, setApproved] = useState<string | null>(null);

  const run = useCallback(async () => {
    setBusy(true);
    setError(null);
    setReport(null);
    setApproved(null);
    try {
      const params = JSON.parse(paramsText || '{}');
      const r = await api.runPreflight({ project_id: projectId, operation, engine, deployment_id: deploymentId, params });
      setReport(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }, [projectId, operation, engine, deploymentId, paramsText]);

  const approve = useCallback(async () => {
    if (!report || !report.approval_digest) return;
    setBusy(true);
    setError(null);
    try {
      const params = JSON.parse(paramsText || '{}');
      const res = await api.approvePreflight(report.approval_digest, {
        project_id: projectId, operation, engine, deployment_id: deploymentId, params,
      });
      setApproved(res.digest);
    } catch (err) {
      if (err instanceof api.CreatorApiError && err.status === 409) {
        setError(t('The plan changed since preflight; re-run preflight and approve again.'));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  }, [report, paramsText, projectId, operation, engine, deploymentId]);

  return (
    <div className="fs-creator__card">
      <h4>{t('Preflight')}</h4>
      <form onSubmit={(e) => { e.preventDefault(); void run(); }} className="fs-creator__preflight-form">
        <label>
          {t('operation')}
          <input value={operation} onChange={(e) => setOperation(e.target.value)} aria-label={t('operation')} data-testid="creator-preflight-operation" />
        </label>
        <label>
          {t('engine')}
          <input value={engine} onChange={(e) => setEngine(e.target.value)} aria-label={t('engine')} />
        </label>
        <label>
          {t('deployment_id')}
          <select value={deploymentId} onChange={(e) => setDeploymentId(e.target.value)} aria-label={t('deployment_id')}>
            <option value="">{t('(none)')}</option>
            {(deployments ?? []).map((d) => <option key={d.deployment_id} value={d.deployment_id}>{d.deployment_id}</option>)}
          </select>
        </label>
        <label>
          {t('params (JSON)')}
          <textarea value={paramsText} onChange={(e) => setParamsText(e.target.value)} rows={3} aria-label={t('params JSON')} />
        </label>
        <Button type="submit" size="sm" variant="primary" label={t('Run preflight')} loading={busy} disabled={busy || !projectId || !operation || !engine} testId="creator-run-preflight" />
      </form>

      {error && <p className="fs-creator__error" role="alert">{error}</p>}

      {report && (
        <div className="fs-creator__preflight-report" data-testid="creator-preflight-report">
          <p>{t('Admission: {fits}', { fits: report.admission.fits ? t('fits') : t('does not fit') })} — {report.admission.reason}</p>
          <p>{t('Estimate:')} {report.estimate.seconds ?? '—'}s · {bytesLabel(report.estimate.vram_bytes)} {t('VRAM')} · {report.estimate.cost_usd != null ? `$${report.estimate.cost_usd}` : '—'}</p>
          <p>{t('Budget verdict: {v}', { v: report.budget.verdict })} {report.budget.reason}</p>
          {report.missing.length > 0 && (
            <ul className="fs-creator__missing">
              {report.missing.map((m, i) => <li key={i}>{m.reason ?? JSON.stringify(m)}</li>)}
            </ul>
          )}
          {report.requires_approval && !approved && (
            <Button size="sm" variant="primary" label={t('Approve')} onClick={() => void approve()} loading={busy} testId="creator-approve" />
          )}
          {approved && <p className="fs-creator__ok">{t('Approved.')}</p>}
        </div>
      )}
    </div>
  );
}

// ── screen ───────────────────────────────────────────────────────────────

export function CreatorScreen() {
  const [params, setParams] = useSearchParams();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectIdState] = useState(params.get('project') ?? '');
  const [caps, setCaps] = useState<api.CreatorCapabilities | 'disabled' | null>(null);
  const [libraryItems, setLibraryItems] = useState<api.LibraryItem[]>([]);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [libraryError, setLibraryError] = useState<string | null>(null);
  const [filter, setFilter] = useState<{ kind: string; q: string }>({ kind: '', q: '' });
  const [docId, setDocId] = useState<string | null>(null);
  const [doc, setDoc] = useState<api.CreatorDocument | null>(null);
  const [docLoading, setDocLoading] = useState(false);
  const [docError, setDocError] = useState<string | null>(null);
  const [history, setHistory] = useState<api.CommandHistoryEntry[]>([]);
  const [resources, setResources] = useState<api.ResourcesSnapshot | null>(null);
  const [deployments, setDeployments] = useState<api.DeploymentSummary[] | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const draftLoaded = useRef(false);

  const setProjectId = useCallback((id: string) => {
    setProjectIdState(id);
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (id) next.set('project', id); else next.delete('project');
      return next;
    }, { replace: true });
  }, [setParams]);

  // Projects, once.
  useEffect(() => {
    const controller = new AbortController();
    listProjects(controller.signal).then(setProjects).catch(() => setProjects([]));
    return () => controller.abort();
  }, []);

  // Restore the per-project draft (UX06) the first time a project is known.
  useEffect(() => {
    if (!projectId || draftLoaded.current) return;
    draftLoaded.current = true;
    const draft = readDraft(projectId);
    if (draft.docId) setDocId(draft.docId);
    setFilter(draft.libraryFilter);
  }, [projectId]);

  // Persist the draft on every meaningful change.
  useEffect(() => {
    if (!projectId) return;
    writeDraft(projectId, { docId, panelWidths: { left: 280, right: 320 }, libraryFilter: filter });
  }, [projectId, docId, filter]);

  // Capability flag: 404 → disabled message; anything else → the manifest.
  useEffect(() => {
    if (!projectId) { setCaps(null); return; }
    const controller = new AbortController();
    api.getCapabilities(projectId, controller.signal)
      .then((c) => setCaps(c))
      .catch((err) => setCaps(err instanceof api.CreatorApiError && err.status === 404 ? 'disabled' : 'disabled'));
    return () => controller.abort();
  }, [projectId]);

  const enabled = caps !== null && caps !== 'disabled';

  // Library.
  const reloadLibrary = useCallback(() => {
    if (!projectId || !enabled) return;
    setLibraryLoading(true);
    setLibraryError(null);
    api.listLibrary(projectId, filter)
      .then((r) => setLibraryItems(r.items))
      .catch((err) => setLibraryError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLibraryLoading(false));
  }, [projectId, enabled, filter]);
  useEffect(() => { reloadLibrary(); }, [reloadLibrary]);

  // Active document.
  const reloadDoc = useCallback(() => {
    if (!docId || !enabled) return;
    setDocLoading(true);
    setDocError(null);
    Promise.all([api.getDocument(docId), api.getDocumentHistory(docId)])
      .then(([d, h]) => { setDoc(d); setHistory(h.commands); })
      .catch((err) => setDocError(err instanceof Error ? err.message : String(err)))
      .finally(() => setDocLoading(false));
  }, [docId, enabled]);
  useEffect(() => { reloadDoc(); }, [reloadDoc]);

  // Resources + deployments (right rail), once Creator is enabled.
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    api.getResources(controller.signal).then(setResources).catch(() => setResources(null));
    api.listKnownModels(controller.signal).then((r) => setDeployments(r.deployments)).catch(() => setDeployments([]));
    return () => controller.abort();
  }, [enabled]);

  const openLibraryItem = useCallback((item: api.LibraryItem) => {
    setDocId(item.id);
  }, []);

  const createDoc = useCallback(async (kind: string) => {
    if (!projectId) return;
    try {
      const created = await api.createDocument(projectId, kind, defaultContentFor(kind));
      setDocId(created.id);
      setDoc(created);
      setToast(t('New {kind} document created.', { kind }));
    } catch (err) {
      setDocError(err instanceof Error ? err.message : String(err));
    }
  }, [projectId]);

  const patchDoc = useCallback(async (commandId: string, expectedRevision: number, patchJson: string) => {
    if (!docId) return;
    const patch = JSON.parse(patchJson) as Record<string, unknown>;
    const result = await api.applyCommand(docId, commandId, expectedRevision, { type: 'patch_content', patch });
    setDoc(result.document);
    setHistory((prev) => [{ command_id: commandId, applied: result.applied }, ...prev]);
    setToast(result.deduped ? t('Command already applied (deduped).') : t('Command applied.'));
  }, [docId]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const kinds = useMemo(() => (caps && caps !== 'disabled' ? caps.document_kinds : []), [caps]);

  return (
    <div className="fs-creator" data-testid="creator-screen">
      <header className="fs-creator__top">
        <h1>{t('Creator')}</h1>
        <ProjectSelector projects={projects} value={projectId} onChange={setProjectId} />
      </header>

      {!projectId && (
        <EmptyState icon={Sparkles} title={t('Choose a project')} body={t('Creator works inside a project — pick one above.')} />
      )}

      {projectId && caps === null && <Skeleton label={t('Checking Creator')} count={3} height="20px" />}

      {projectId && caps === 'disabled' && (
        <EmptyState
          icon={FileWarning}
          title={t('Creator is disabled')}
          body={t('Turn it on in Settings: the "creator_enabled" setting.')}
          tone="denied"
        />
      )}

      {projectId && enabled && (
        <div className="fs-creator__grid" role="group" aria-label={t('Creator workspace')}>
          <LibraryPanel
            projectId={projectId}
            filter={filter}
            onFilterChange={setFilter}
            activeDocId={docId}
            onOpen={openLibraryItem}
            items={libraryItems}
            loading={libraryLoading}
            error={libraryError}
          />
          <DocumentPanel
            doc={doc}
            history={history}
            loading={docLoading}
            error={docError}
            onCreate={(kind) => void createDoc(kind)}
            onPatch={patchDoc}
            onReload={reloadDoc}
            projectId={projectId}
            kinds={kinds}
            onDocUpdated={setDoc}
          />
          <aside className="fs-creator__right" aria-label={t('Capabilities and resources')}>
            <ResourcesCard resources={resources} />
            <DeploymentsCard deployments={deployments} />
            <PreflightCard projectId={projectId} deployments={deployments} />
          </aside>
        </div>
      )}

      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
