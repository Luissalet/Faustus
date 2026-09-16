import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton, Toast } from '../../components';
import { fmtGb } from '../../adapters/localModels';
import * as creatorApi from '../../adapters/creator';
import * as api from '../../adapters/creator_models';
import { t } from '../../i18n';

/**
 * WP08 — Model Explorer y comparación (UX11, MOD07, MOD10, MOD11, MOD12,
 * MOD16, MOD17), a tab inside `CreatorScreen.tsx`.
 *
 * Filters by task/capability/evidence over `GET /api/creator/models/
 * explorer`; a ficha per deployment (`GET .../explorer/{id}`); a side-by-side
 * compare of 2-3 deployments (`POST .../compare`) that renders every cell's
 * `basis` explicitly — a KNOWN/ANNOUNCED/MEASURED/ESTIMATED badge next to
 * the value, and the literal word "unknown" (never a blank cell, a dash
 * standing in for zero, or an estimate quietly presented as a measurement:
 * MOD-11's two closing criteria) when there is no evidence. "Usar para esta
 * tarea" writes the choice into the project's CreatorProfile via the
 * existing `putCreatorProfile` (WP02) — no new persistence here.
 */

const BASIS_WORD: Record<string, string> = {
  known: 'known',
  announced: 'announced',
  unsupported: 'unsupported',
  unknown: 'unknown',
  measured: 'measured',
  estimated: 'estimated',
};

function BasisBadge({ basis }: { basis: string }) {
  const word = BASIS_WORD[basis] ?? basis;
  return <span className={`fs-model-explorer__basis fs-model-explorer__basis--${basis}`}>{t(word)}</span>;
}

function cellText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? t('yes') : t('no');
  if (typeof value === 'number') return String(value);
  return String(value);
}

// ── filters + list ───────────────────────────────────────────────────────

interface Filters {
  q: string;
  task: string;
  capability: string;
  evidence: string; // '' | 'known' | 'announced' | 'unknown'
}

const EMPTY_FILTERS: Filters = { q: '', task: '', capability: '', evidence: '' };

function matchesFilters(row: api.ExplorerRow, filters: Filters): boolean {
  if (filters.q) {
    const needle = filters.q.toLowerCase();
    const haystack = `${row.model_id} ${row.family} ${row.vendor}`.toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  if (filters.capability) {
    if (!row.known_axes.includes(filters.capability) && !row.announced_axes.includes(filters.capability)) return false;
  }
  if (filters.evidence === 'known' && row.known_axes.length === 0) return false;
  if (filters.evidence === 'announced' && row.announced_axes.length === 0 && row.known_axes.length === 0) return false;
  return true;
}

function ExplorerFilters({ filters, onChange }: { filters: Filters; onChange: (f: Filters) => void }) {
  return (
    <div className="fs-model-explorer__filters">
      <input
        type="search"
        placeholder={t('Search model or family…')}
        value={filters.q}
        onChange={(e) => onChange({ ...filters, q: e.target.value })}
        aria-label={t('Search models')}
      />
      <select
        value={filters.capability}
        onChange={(e) => onChange({ ...filters, capability: e.target.value })}
        aria-label={t('Filter by capability')}
      >
        <option value="">{t('Any capability')}</option>
        {['vision_in', 'image_out', 'image_edit', 'region_edit', 'controlnet', 'upscale',
          'audio_in', 'audio_out', 'video_in', 'video_out', 'music', 'tts', 'asr'].map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>
      <select
        value={filters.evidence}
        onChange={(e) => onChange({ ...filters, evidence: e.target.value })}
        aria-label={t('Filter by evidence')}
      >
        <option value="">{t('Any evidence')}</option>
        <option value="known">{t('Has known (tested) capabilities')}</option>
        <option value="announced">{t('Has at least announced capabilities')}</option>
      </select>
    </div>
  );
}

function ExplorerList({
  rows, loading, error, selected, onToggleSelect, onOpen,
}: {
  rows: api.ExplorerRow[];
  loading: boolean;
  error: string | null;
  selected: string[];
  onToggleSelect: (id: string) => void;
  onOpen: (id: string) => void;
}) {
  if (loading) return <Skeleton label={t('Loading models')} count={4} height="48px" />;
  if (error) return <EmptyState title={t('Could not load the Model Explorer')} body={error} />;
  if (rows.length === 0) {
    return <EmptyState title={t('No known deployments yet')} body={t('Deployments appear here once Faustus has resolved or calibrated them (WP06).')} />;
  }
  return (
    <ul className="fs-model-explorer__list" role="listbox" aria-label={t('Deployments')}>
      {rows.map((row) => (
        <li key={row.deployment_id} className="fs-model-explorer__row">
          <label className="fs-model-explorer__row-select">
            <input
              type="checkbox"
              checked={selected.includes(row.deployment_id)}
              onChange={() => onToggleSelect(row.deployment_id)}
              disabled={!selected.includes(row.deployment_id) && selected.length >= 3}
              aria-label={t('Select {model} for comparison', { model: row.model_id })}
            />
          </label>
          <button
            type="button"
            className="fs-model-explorer__row-open"
            onClick={() => onOpen(row.deployment_id)}
            data-testid={`explorer-row-${row.deployment_id}`}
          >
            <b>{row.model_id || row.family || row.deployment_id}</b>
            <span className="fs-creator__muted">
              {row.vendor} · {row.engine?.kind ?? '—'} · {row.context_tokens ? `${row.context_tokens.toLocaleString()} tok` : t('context unknown')}
            </span>
            <span className="fs-creator__muted">
              {t('{k} known, {a} announced', { k: row.known_axes.length, a: row.announced_axes.length })}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

// ── ficha (detail) ───────────────────────────────────────────────────────

function FootprintCard({ footprint }: { footprint: api.FootprintBlock }) {
  return (
    <div className="fs-creator__card">
      <h4>{t('Footprint & VRAM')}</h4>
      {footprint.size_bytes > 0 ? (
        <p>
          {fmtGb(footprint.size_bytes)} <BasisBadge basis={footprint.basis} />
          {footprint.vram_state && <> · {t(footprint.vram_state)}</>}
        </p>
      ) : (
        <p className="fs-creator__muted">{t('unknown')} — {footprint.reason || t('no evidence')}</p>
      )}
      {footprint.vram_note && <p className="fs-creator__muted">{footprint.vram_note}</p>}
    </div>
  );
}

function CapabilityAxesCard({ axes }: { axes: Record<string, api.CapabilityAxisEvidence> }) {
  const entries = Object.entries(axes);
  return (
    <div className="fs-creator__card">
      <h4>{t('Capabilities')}</h4>
      <ul className="fs-model-explorer__axes">
        {entries.map(([axis, ev]) => (
          <li key={axis}>
            <span>{axis}</span>
            <BasisBadge basis={ev.status} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function ParamSchemasCard({ schemas }: { schemas: api.ExplorerEntry['param_schemas'] }) {
  return (
    <div className="fs-creator__card">
      <h4>{t('Parameter contracts')}</h4>
      {schemas.length === 0 ? (
        <p className="fs-creator__muted">{t('No registered parameter schema for this engine yet.')}</p>
      ) : (
        <ul>
          {schemas.map((s) => (
            <li key={`${s.engine}-${s.task}`}><code>{s.engine}</code> / <code>{s.task}</code></li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DetailPanel({
  entry, loading, error, projectId, onUseForTask, useTaskBusy, useTaskDone,
}: {
  entry: api.ExplorerEntry | null;
  loading: boolean;
  error: string | null;
  projectId: string;
  onUseForTask: (deploymentId: string, task: string) => void;
  useTaskBusy: boolean;
  useTaskDone: string | null;
}) {
  const [task, setTask] = useState('chat.completions');

  if (loading) return <Skeleton label={t('Loading model')} count={5} height="18px" />;
  if (error) return <EmptyState title={t('Could not load this model')} body={error} />;
  if (!entry) return <EmptyState title={t('Select a model')} body={t('Pick a deployment from the list to see its full ficha.')} />;

  const spec = entry.model_spec as Record<string, unknown>;
  return (
    <div className="fs-model-explorer__detail" data-testid="explorer-detail">
      <header>
        <h3>{String(spec.model_id ?? entry.deployment_id)}</h3>
        <p className="fs-creator__muted">
          {String(spec.vendor ?? '')} · {String(spec.family ?? '')} · {String(spec.parameter_size ?? t('size unknown'))} · {String(spec.quantization ?? '')}
        </p>
        <p className="fs-creator__muted">{t('License:')} {String(spec.license ?? t('unknown'))}</p>
      </header>

      <div className="fs-model-explorer__use-for-task">
        <label>
          {t('Task')}
          <input value={task} onChange={(e) => setTask(e.target.value)} aria-label={t('Task id, e.g. chat.completions')} />
        </label>
        <Button
          size="sm"
          variant="primary"
          label={t('Use for this task')}
          onClick={() => onUseForTask(entry.deployment_id, task)}
          loading={useTaskBusy}
          disabled={!projectId || useTaskBusy}
          testId="explorer-use-for-task"
        />
        {useTaskDone === entry.deployment_id && <span className="fs-creator__ok">{t('Saved to project preferences.')}</span>}
      </div>

      <FootprintCard footprint={entry.footprint} />
      <CapabilityAxesCard axes={entry.capability_profile.axes} />
      <ParamSchemasCard schemas={entry.param_schemas} />

      {entry.legacy_calibration && (
        <div className="fs-creator__card">
          <h4>{t('Legacy calibration (pre-deployment-id)')}</h4>
          <p className="fs-creator__muted">{t('Older evidence, scoped to this manifest key, not to this exact deployment.')}</p>
        </div>
      )}
    </div>
  );
}

// ── compare ──────────────────────────────────────────────────────────────

function CompareTablePanel({ table }: { table: api.CompareTable }) {
  return (
    <div className="fs-model-explorer__compare" data-testid="explorer-compare-table">
      {table.unknown_deployment_ids.length > 0 && (
        <p className="fs-creator__error" role="alert">
          {t('Never heard of: {ids}', { ids: table.unknown_deployment_ids.join(', ') })}
        </p>
      )}
      <table>
        <thead>
          <tr>
            <th>{t('Field')}</th>
            {table.deployment_ids.map((id) => <th key={id}>{id}</th>)}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={row.field}>
              <td>{t(row.label)}</td>
              {table.deployment_ids.map((id) => {
                const cell = row.cells[id];
                return (
                  <td key={id}>
                    {cellText(cell?.value)} <BasisBadge basis={cell?.basis ?? 'unknown'} />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── screen ───────────────────────────────────────────────────────────────

export function ModelExplorer({ projectId }: { projectId: string }) {
  const [rows, setRows] = useState<api.ExplorerRow[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);

  const [openId, setOpenId] = useState<string | null>(null);
  const [entry, setEntry] = useState<api.ExplorerEntry | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [selected, setSelected] = useState<string[]>([]);
  const [compareTask, setCompareTask] = useState('chat.completions');
  const [compareTable, setCompareTable] = useState<api.CompareTable | null>(null);
  const [compareBusy, setCompareBusy] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  const [useTaskBusy, setUseTaskBusy] = useState(false);
  const [useTaskDone, setUseTaskDone] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setListLoading(true);
    setListError(null);
    api.listExplorer(controller.signal)
      .then((r) => setRows(r.deployments))
      .catch((err) => setListError(err instanceof Error ? err.message : String(err)))
      .finally(() => setListLoading(false));
    return () => controller.abort();
  }, []);

  const filteredRows = useMemo(() => rows.filter((r) => matchesFilters(r, filters)), [rows, filters]);

  const openEntry = useCallback((id: string) => {
    setOpenId(id);
    setDetailLoading(true);
    setDetailError(null);
    setUseTaskDone(null);
    api.getExplorerEntry(id)
      .then(setEntry)
      .catch((err) => setDetailError(err instanceof Error ? err.message : String(err)))
      .finally(() => setDetailLoading(false));
  }, []);

  const toggleSelect = useCallback((id: string) => {
    setSelected((prev) => {
      if (prev.includes(id)) return prev.filter((x) => x !== id);
      if (prev.length >= 3) return prev; // MOD-11: 2-3 deployments; the picker never silently drops the explicit selection past 3, it just stops adding
      return [...prev, id];
    });
  }, []);

  const runCompare = useCallback(async () => {
    setCompareBusy(true);
    setCompareError(null);
    setCompareTable(null);
    try {
      const table = await api.compareDeployments(selected, compareTask);
      setCompareTable(table);
    } catch (err) {
      setCompareError(err instanceof Error ? err.message : String(err));
    } finally {
      setCompareBusy(false);
    }
  }, [selected, compareTask]);

  const useForTask = useCallback(async (deploymentId: string, task: string) => {
    if (!projectId || !task) return;
    setUseTaskBusy(true);
    try {
      const current = await creatorApi.getCreatorProfile(projectId);
      const routing = (current.profile.routing_by_task && typeof current.profile.routing_by_task === 'object')
        ? { ...(current.profile.routing_by_task as Record<string, unknown>) }
        : {};
      routing[task] = deploymentId;
      await creatorApi.putCreatorProfile(projectId, { ...current.profile, routing_by_task: routing });
      setUseTaskDone(deploymentId);
      setToast(t('{model} set as the preference for "{task}".', { model: deploymentId, task }));
    } catch (err) {
      setToast(err instanceof Error ? err.message : String(err));
    } finally {
      setUseTaskBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3000);
    return () => window.clearTimeout(timer);
  }, [toast]);

  return (
    <div className="fs-model-explorer" data-testid="model-explorer">
      <ExplorerFilters filters={filters} onChange={setFilters} />

      <div className="fs-model-explorer__body">
        <section className="fs-model-explorer__left" aria-label={t('Model list')}>
          <ExplorerList
            rows={filteredRows}
            loading={listLoading}
            error={listError}
            selected={selected}
            onToggleSelect={toggleSelect}
            onOpen={openEntry}
          />
        </section>

        <section className="fs-model-explorer__right" aria-label={t('Model detail')}>
          <DetailPanel
            entry={openId === entry?.deployment_id ? entry : null}
            loading={detailLoading}
            error={detailError}
            projectId={projectId}
            onUseForTask={useForTask}
            useTaskBusy={useTaskBusy}
            useTaskDone={useTaskDone}
          />
        </section>
      </div>

      <section className="fs-model-explorer__compare-controls" aria-label={t('Compare deployments')}>
        <h4>{t('Compare ({n}/3 selected)', { n: selected.length })}</h4>
        <label>
          {t('Task')}
          <input value={compareTask} onChange={(e) => setCompareTask(e.target.value)} aria-label={t('Compare task')} />
        </label>
        <Button
          size="sm"
          variant="primary"
          label={t('Compare')}
          onClick={() => void runCompare()}
          loading={compareBusy}
          disabled={selected.length < 2 || compareBusy}
          testId="explorer-run-compare"
        />
        {compareError && <p className="fs-creator__error" role="alert">{compareError}</p>}
        {compareTable && <CompareTablePanel table={compareTable} />}
      </section>

      {toast && <Toast>{toast}</Toast>}
    </div>
  );
}
