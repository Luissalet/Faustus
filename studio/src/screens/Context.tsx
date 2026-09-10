import {
  AlertTriangle,
  Boxes,
  Database,
  Gauge,
  Link2,
  Pencil,
  Plus,
  RefreshCw,
  ScanSearch,
  Search,
  ShieldAlert,
  Trash2,
  Unlink,
  X,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Skeleton } from '../components';
import { ApiError } from '../adapters/api';
import { parseStamp, relativeTime } from '../adapters/home';
import { getSettings } from '../adapters/settings';
import {
  BLOCK_SCOPES,
  BLOCK_TYPES,
  CODE_INDEX_STALE_S,
  ContextRefusal,
  PACKET_PAGE,
  attachBlock,
  codeIndexStale,
  createBlock,
  deleteBlock,
  detachBlock,
  engineMode,
  formatBytes,
  invalidateContextCache,
  loadBlockAudit,
  loadBlocks,
  loadCodeIndexStatus,
  loadDiagnostics,
  loadExperiences,
  loadFindings,
  loadManifest,
  loadPackets,
  loadSelectionControls,
  oldestPacket,
  refreshBlocker,
  refreshCodeIndex,
  runMaintenance,
  searchCode,
  sendExperienceFeedback,
  setSelectionControl,
  supersedeChain,
  symbolRef,
  unsetSelectionControl,
  updateBlock,
  verdictReading,
  type Block,
  type BlockDraft,
  type CodeSymbol,
  type EngineMode,
  type Experience,
  type Finding,
  type MaintenanceRun,
  type PacketRow,
  type RefreshBlock,
  type RefreshReport,
  type SelectionControls,
} from '../adapters/context';
import { locale, t, tn } from '../i18n';
import './projects.css';
import './home.css';
import './context.css';

/**
 * Context Engine (§19, §24).
 *
 * The subsystem exists so that "nobody can tell what the model was told" stops
 * being true, and this screen is where that promise is either kept or not. So
 * it is built around the two questions people actually arrive with — "why did
 * it know that?" and "why did it not read that file?" — and everything else on
 * it is there to make those two answerable: the ledger with its manifests, the
 * blocks that get pasted into every prompt, the experiences the ranker trusts,
 * and the index the agent searches instead of guessing.
 *
 * Three rules run through the rendering:
 *
 * * **A verdict is never painted as a success.** `unproved` and `contradicted`
 *   read differently from `proved` at a glance, and never by colour alone.
 * * **Degradation is said, not hidden.** A degraded packet carries a mark in
 *   the list and the compiler's own warnings in the pane.
 * * **A refusal shows the server's sentence and the field it names.** The
 *   engine answers a rejection with `{"ok": false, "error": {…}}`; that message
 *   is the only thing that says which character looked like a credential.
 */

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'packets', label: 'Packets' },
  { id: 'blocks', label: 'Blocks' },
  { id: 'knowledge', label: 'Knowledge' },
  { id: 'code', label: 'Code index' },
] as const;

type TabId = (typeof TABS)[number]['id'];

/* ── shared pieces ─────────────────────────────────────────────────────── */

/**
 * One request, re-run when `key` changes and on demand.
 *
 * `key` rather than a dependency array because every panel here is "one URL,
 * one answer": the query string *is* the identity of the request, and an
 * aborted controller is what keeps a fast typist from rendering the answer to
 * a query they have already replaced.
 */
function useRemote<T>(key: string, load: (signal: AbortSignal) => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const latest = useRef(load);
  latest.current = load;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void latest.current(controller.signal).then(
      (value) => {
        if (controller.signal.aborted) return;
        setData(value);
        setError(null);
        setLoading(false);
      },
      (failure: unknown) => {
        if (controller.signal.aborted) return;
        setError(failure);
        setLoading(false);
      },
    );
    return () => controller.abort();
  }, [key, nonce]);

  return { data, error, loading, reload: () => setNonce((n) => n + 1) };
}

/**
 * What went wrong, in the server's own words.
 *
 * A `ContextRefusal` names the field, and that field is the actionable half of
 * the sentence: "looks like it carries a credential (matched 'api_key')" is
 * only useful when you also know it was `block.content`.
 */
function Problem({ error, what }: { error: unknown; what: string }) {
  if (!error) return null;
  if (error instanceof ContextRefusal) {
    return (
      <p className="fs-notice fs-ctx__problem" data-tone="warning" role="alert" data-testid="context-refusal">
        <ShieldAlert size={13} aria-hidden="true" />
        <span>
          {error.message}
          <code className="fs-ctx__path">{error.path}</code>
          {error.revision !== undefined && (
            <span className="fs-ctx__hint">
              {t('It is at revision {n} now: reload it and re-apply your change.', { n: error.revision })}
            </span>
          )}
        </span>
      </p>
    );
  }
  const detail = error instanceof ApiError || error instanceof Error ? error.message : '';
  return (
    <p className="fs-notice fs-ctx__problem" data-tone="warning" role="alert">
      <AlertTriangle size={13} aria-hidden="true" />
      <span>{detail ? `${what}: ${detail}` : what}</span>
    </p>
  );
}

/** A number with the question it answers under it. */
function Stat({
  label,
  value,
  hint,
  tone,
  testId,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: 'warning' | 'good';
  testId?: string;
}) {
  return (
    <div className="fs-ctx__stat" data-tone={tone} data-testid={testId}>
      <span className="fs-ctx__stat-label">{label}</span>
      <strong className="fs-ctx__stat-value">{value}</strong>
      {hint && <span className="fs-ctx__stat-hint">{hint}</span>}
    </div>
  );
}

function Panel({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="fs-panel fs-ctx__panel">
      <header className="fs-ctx__panel-head">
        <h3 className="fs-panel__label">{title}</h3>
        {actions}
      </header>
      {children}
    </section>
  );
}

/** A count bar: the reason, the number, and how much of the whole it is. */
function Share({ label, count, share }: { label: string; count: string; share: number }) {
  return (
    <div className="fs-ctx__share">
      <span className="fs-ctx__share-label">{label}</span>
      <span className="fs-ctx__share-track" aria-hidden="true">
        <span className="fs-ctx__share-fill" style={{ inlineSize: `${Math.min(100, share)}%` }} />
      </span>
      <span className="fs-ctx__share-count">
        {count} · {share}%
      </span>
    </div>
  );
}

/**
 * An absolute stamp, read through `parseStamp` rather than `Date.parse`: the
 * server writes naive ISO stamps that are UTC without saying so, and reading
 * them as local time is how a packet from two minutes ago is dated two hours
 * ago in Madrid. That rule is already owned once, in adapters/home.
 */
function stamp(value: string): string {
  const at = parseStamp(value);
  if (!at) return value || '—';
  return new Date(at).toLocaleString(locale(), { dateStyle: 'short', timeStyle: 'short' });
}

/* ── Overview ──────────────────────────────────────────────────────────── */

const MODE_LABEL: Record<EngineMode, string> = {
  live: 'On — the engine decides what the model is told',
  shadow: 'Shadow — it compiles a packet, and nothing is delivered',
  off: 'Off — nothing is compiled and no packet is recorded',
};

function Overview({ projectId, workspace, mode }: { projectId: string; workspace: string; mode: EngineMode | null }) {
  const diagnostics = useRemote(`diag:${projectId}`, (signal) => loadDiagnostics(projectId, signal));
  const ledger = useRemote(`ledger:${projectId}`, (signal) => loadPackets(projectId, PACKET_PAGE, signal));
  const index = useRemote(`index:${workspace}:${projectId}`, (signal) =>
    loadCodeIndexStatus(workspace, projectId, signal),
  );

  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [ran, setRan] = useState<MaintenanceRun[] | null>(null);
  const [ranError, setRanError] = useState<unknown>(null);

  // PERF-03: "liberar caché" — the working set is process-wide, so clearing
  // it here is exactly what the diagnostics Stat above just measured.
  const [clearingCache, setClearingCache] = useState(false);
  const [clearedCache, setClearedCache] = useState<number | null>(null);
  const [clearCacheError, setClearCacheError] = useState<unknown>(null);

  async function clearCache() {
    setClearingCache(true);
    setClearCacheError(null);
    try {
      const dropped = await invalidateContextCache(projectId);
      setClearedCache(dropped);
      diagnostics.reload();
    } catch (failure) {
      setClearCacheError(failure);
    } finally {
      setClearingCache(false);
    }
  }

  const data = diagnostics.data;
  const oldest = useMemo(() => oldestPacket(ledger.data ?? [], PACKET_PAGE), [ledger.data]);
  const stale = index.data ? codeIndexStale(index.data) : false;

  async function run() {
    setRunning(true);
    setRanError(null);
    try {
      const result = await runMaintenance(workspace, projectId);
      setRan(result.results);
      setConfirming(false);
      diagnostics.reload();
      index.reload();
    } catch (failure) {
      setRanError(failure);
    } finally {
      setRunning(false);
    }
  }

  if (diagnostics.loading && !data) return <Skeleton label={t('Reading the diagnostics')} count={4} height="64px" />;
  if (!data) return <Problem error={diagnostics.error} what={t('The diagnostics could not be read')} />;

  const cache = data.cache;
  const sources = data.sources;

  return (
    <div className="fs-ctx__tab" data-testid="context-overview">
      <div className="fs-ctx__stats">
        <Stat
          label={t('Engine')}
          value={mode === null ? t('unknown') : mode === 'live' ? t('on') : mode === 'shadow' ? t('shadow') : t('off')}
          hint={mode === null ? t('the settings could not be read from here') : t(MODE_LABEL[mode])}
          tone={mode === 'live' ? 'good' : undefined}
          testId="context-mode"
        />
        <Stat
          label={t('Packets in the ledger')}
          value={String(data.packets)}
          hint={tn(data.receipts, '{n} receipt says what happened after delivery', '{n} receipts say what happened after delivery')}
        />
        <Stat
          label={t('Degraded')}
          value={`${data.degraded} · ${data.degradedPct}%`}
          hint={data.degraded ? t('these packets could not carry everything they should have') : t('nothing was dropped that mattered')}
          tone={data.degraded > 0 ? 'warning' : undefined}
          testId="context-degraded"
        />
        <Stat
          label={t('Average packet')}
          value={tn(data.avgTokens, '{n} token', '{n} tokens')}
          hint={t('{pct}% of the input budget it was given', { pct: data.avgBudgetPct })}
        />
        <Stat label={t('Store on disk')} value={formatBytes(data.storeBytes)} hint={t('the database, its write-ahead log included')} />
        <Stat
          label={t('Working set hit rate')}
          value={cache.error ? t('unreadable') : `${Math.round(cache.hitRate * 1000) / 10}%`}
          hint={
            cache.error
              ? cache.error
              : t('{entries} entries · {evictions} evicted', { entries: cache.entries, evictions: cache.evictions })
          }
          tone={cache.error ? 'warning' : undefined}
          testId="context-cache-hit-rate"
        />
        <Stat
          label={t('Oldest packet kept')}
          value={oldest.at ? relativeTime(oldest.at) : '—'}
          hint={
            oldest.capped
              ? t('the oldest of the {n} newest rows; the ledger may hold more', { n: PACKET_PAGE })
              : t('the whole ledger, pruned by age on the maintenance pass')
          }
        />
        <Stat
          label={t('Code index')}
          value={index.data ? tn(index.data.symbols, '{n} symbol', '{n} symbols') : t('unreadable')}
          hint={
            index.data
              ? stale
                ? t('out of date: last indexed {when}', { when: relativeTime(index.data.lastIndexedAt) || t('never') })
                : t('{files} files · indexed {when}', {
                    files: index.data.files,
                    when: relativeTime(index.data.lastIndexedAt) || t('never'),
                  })
              : t('the index could not be read')
          }
          tone={stale ? 'warning' : undefined}
          testId="context-index-freshness"
        />
      </div>

      {sources.error ? (
        <p className="fs-notice" data-tone="warning">
          {t('The source registry could not be read')}: {sources.error}
        </p>
      ) : sources.unavailable.length > 0 ? (
        <p className="fs-notice" data-tone="warning" data-testid="context-sources-unavailable">
          {tn(
            sources.unavailable.length,
            '{n} declared source is not available, so nothing it holds can reach a packet:',
            '{n} declared sources are not available, so nothing they hold can reach a packet:',
          )}{' '}
          <code>{sources.unavailable.join(', ')}</code>
        </p>
      ) : (
        <p className="fs-muted">
          {tn(
            sources.built.length,
            'The only declared source is built and reachable.',
            'All {n} declared sources are built and reachable.',
          )}
        </p>
      )}

      <div className="fs-ctx__columns">
        <Panel title={t('Why anything was left out')}>
          {data.omissions.length === 0 ? (
            <p className="fs-muted">{t('Nothing has been left out of a packet in this scope.')}</p>
          ) : (
            <div className="fs-ctx__shares">
              {data.omissions.map((group) => (
                <Share key={group.reason} label={group.reason} count={String(group.count)} share={group.pct} />
              ))}
            </div>
          )}
        </Panel>

        <Panel title={t('Rows per table')}>
          <div className="fs-ctx__table-wrap">
            <table className="fs-ctx__table">
              <thead>
                <tr>
                  <th>{t('Table')}</th>
                  <th className="fs-ctx__num">{t('Rows')}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(data.tables)
                  .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
                  .map(([name, rows]) => (
                    <tr key={name}>
                      <td>
                        <code>{name}</code>
                      </td>
                      <td className="fs-ctx__num">{rows < 0 ? t('unreadable') : rows}</td>
                    </tr>
                  ))}
                {Object.keys(data.tables).length === 0 && (
                  <tr>
                    <td colSpan={2} className="fs-ctx__empty-cell">
                      {t('The store has not been created yet: nothing has written to it.')}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>

      <Panel
        title={t('Maintenance')}
        actions={
          <Button
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            label={t('Run maintenance')}
            onClick={() => setConfirming(true)}
            testId="context-run-maintenance"
          />
        }
      >
        <p className="fs-muted fs-ctx__lede">
          {data.due.length > 0
            ? t('Due now: {names}.', { names: data.due.join(', ') })
            : t('Nothing is due: every task has run inside its own interval.')}
        </p>
        <Problem error={ranError} what={t('The maintenance pass could not be started')} />
        <div className="fs-ctx__table-wrap">
          <table className="fs-ctx__table">
            <thead>
              <tr>
                <th>{t('Task')}</th>
                <th>{t('Last run')}</th>
                <th className="fs-ctx__num">{t('Changed')}</th>
                <th>{t('What it did')}</th>
              </tr>
            </thead>
            <tbody>
              {(ran ?? data.lastRun).map((task) => (
                <tr key={task.name} data-ok={task.ok || undefined}>
                  <td>
                    <code>{task.name}</code>
                  </td>
                  <td>{ran ? t('just now') : task.ranAt ? relativeTime(task.ranAt) : t('never')}</td>
                  <td className="fs-ctx__num">{task.changed}</td>
                  <td className="fs-ctx__detail-cell">
                    {!task.ok && <span className="fs-ctx__flag" data-tone="bad">{t('failed')}</span>} {task.detail || '—'}
                  </td>
                </tr>
              ))}
              {(ran ?? data.lastRun).length === 0 && (
                <tr>
                  <td colSpan={4} className="fs-ctx__empty-cell">
                    {t('The pass has never run on this install.')}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel
        title={t('Cache')}
        actions={
          <Button
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            label={t('Free the cache')}
            loading={clearingCache}
            onClick={() => void clearCache()}
            testId="context-clear-cache"
          />
        }
      >
        <Problem error={clearCacheError} what={t('The cache could not be cleared')} />
        <p className="fs-muted fs-ctx__lede">
          {clearedCache === null
            ? t('Forgets every cached retrieval/response entry for this project — a permission, content or template change already invalidates the affected entries on its own; use this to force a recompute regardless.')
            : tn(clearedCache, '{n} cached entry was forgotten.', '{n} cached entries were forgotten.')}
        </p>
      </Panel>

      <SelectionPanel projectId={projectId} />

      <Dialog
        open={confirming}
        onOpenChange={setConfirming}
        title={t('Run the maintenance pass now?')}
        description={t(
          'It prunes the ledger by age, expires findings, refreshes the code index, marks experiences whose files are gone, audits the blocks and vacuums the store. None of that can be undone, and none of it is urgent.',
        )}
        testId="context-maintenance-dialog"
        footer={
          <>
            <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirming(false)} />
            <Button
              variant="primary"
              size="sm"
              label={t('Run it')}
              loading={running}
              onClick={() => void run()}
              testId="context-maintenance-confirm"
            />
          </>
        }
      >
        {!workspace && (
          <p className="fs-muted">
            {t('No workspace is named, so the code index will be skipped. Name one on the Code index tab first if you want it refreshed.')}
          </p>
        )}
      </Dialog>
    </div>
  );
}

/* ── CTX-05: user-controlled retrieval scope ─────────────────────────────── */

/**
 * "No usar esta fuente" / "usar este fragmento", at project scope — never
 * deletes anything it names, only whether retrieval offers it (see
 * `src/context_selection.py`). Session-scoped controls are set from the
 * chat's own composer (a different, foreign screen); this panel is the
 * project-wide default a person manages from Settings-shaped context.
 */
function SelectionPanel({ projectId }: { projectId: string }) {
  const controls = useRemote(`selection:${projectId}`, (signal) => loadSelectionControls(projectId, '', signal));
  const [ref, setRef] = useState('');
  const [kind, setKind] = useState<'exclude' | 'exclude_prefix' | 'pin'>('exclude');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function add() {
    const clean = ref.trim();
    if (!clean) return;
    setBusy(true);
    setError(null);
    try {
      await setSelectionControl(kind, clean, { projectId });
      setRef('');
      controls.reload();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  async function remove(removeKind: 'exclude' | 'exclude_prefix' | 'pin', removeRef: string) {
    setBusy(true);
    setError(null);
    try {
      await unsetSelectionControl(removeKind, removeRef, { projectId });
      controls.reload();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  const data: SelectionControls = controls.data ?? { exclude: [], exclude_prefix: [], pin: [] };
  const rows: Array<{ kind: 'exclude' | 'exclude_prefix' | 'pin'; ref: string }> = [
    ...data.exclude.map((c) => ({ kind: 'exclude' as const, ref: c.ref })),
    ...data.exclude_prefix.map((c) => ({ kind: 'exclude_prefix' as const, ref: c.ref })),
    ...data.pin.map((c) => ({ kind: 'pin' as const, ref: c.ref })),
  ];

  return (
    <Panel title={t('Selective recall')}>
      <p className="fs-muted fs-ctx__lede">
        {t('Exclude a file or folder from retrieval, or pin one to always be offered — for this project. Never deletes the source itself.')}
      </p>
      <Problem error={error} what={t('The control could not be saved')} />
      <div className="fs-ctx__form">
        <label className="fs-ctx__label">
          <span>{t('Kind')}</span>
          <select className="fs-field" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            <option value="exclude">{t('Exclude a source')}</option>
            <option value="exclude_prefix">{t('Exclude a folder')}</option>
            <option value="pin">{t('Pin a source')}</option>
          </select>
        </label>
        <label className="fs-ctx__label">
          <span>{t('Reference')}</span>
          <input
            className="fs-field"
            value={ref}
            onChange={(e) => setRef(e.target.value)}
            placeholder={t('e.g. file:src/app.py')}
          />
        </label>
        <label className="fs-ctx__label">
          <span>&nbsp;</span>
          <Button variant="secondary" size="sm" icon={Plus} label={t('Add')} loading={busy} onClick={() => void add()} testId="context-selection-add" />
        </label>
      </div>
      <div className="fs-ctx__table-wrap">
        <table className="fs-ctx__table">
          <thead>
            <tr>
              <th>{t('Kind')}</th>
              <th>{t('Reference')}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.kind}:${row.ref}`}>
                <td>
                  {row.kind === 'exclude' && t('Excluded source')}
                  {row.kind === 'exclude_prefix' && t('Excluded folder')}
                  {row.kind === 'pin' && t('Pinned source')}
                </td>
                <td>
                  <code>{row.ref}</code>
                </td>
                <td>
                  <IconButton
                    icon={Trash2}
                    label={t('Remove')}
                    size="sm"
                    onClick={() => void remove(row.kind, row.ref)}
                  />
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={3} className="fs-ctx__empty-cell">
                  {t('Nothing is excluded or pinned for this project.')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

/* ── Packets: the ledger, and the manifest behind each row ─────────────── */

function ManifestPane({ packet, onClose }: { packet: PacketRow; onClose: () => void }) {
  const manifest = useRemote(`manifest:${packet.id}`, (signal) => loadManifest(packet.id, signal));
  const data = manifest.data;

  return (
    <aside className="fs-ctx__pane" aria-labelledby="fs-ctx-manifest" data-testid="context-manifest">
      <div className="fs-ctx__pane-head">
        <div>
          <h3 id="fs-ctx-manifest">{t('Why this packet said what it said')}</h3>
          <p className="fs-ctx__pane-sub">
            <code>{packet.id}</code> · {stamp(packet.createdAt)}
          </p>
        </div>
        <IconButton icon={X} label={t('Close the manifest')} size="sm" onClick={onClose} />
      </div>

      {packet.degraded && (
        <p className="fs-notice" data-tone="warning" data-testid="context-degraded-warning">
          <strong>{t('This packet is degraded.')}</strong>{' '}
          {data && data.warnings.length > 0
            ? data.warnings.join(' · ')
            : data && !data.retained
              ? t('The compiler wrote warnings, and they lived beside the manifest that has since been evicted. Recompile the request to read them.')
              : t('The compiler marked it degraded without leaving a warning behind.')}
        </p>
      )}

      {manifest.loading && !data && <Skeleton label={t('Reading the manifest')} count={3} height="40px" />}
      <Problem error={manifest.error} what={t('The manifest could not be read')} />

      {data && !data.retained && (
        <p className="fs-notice" data-testid="context-manifest-evicted">
          {data.note ||
            t('The ledger keeps counts, not rows: the per-item manifest lives in the working set and this one has been evicted.')}
        </p>
      )}

      {data && data.sections.length > 0 && (
        <div className="fs-ctx__shares">
          {data.sections.map((section) => (
            <Share
              key={section.kind}
              label={section.kind}
              count={tn(section.tokens, '{n} token', '{n} tokens')}
              share={section.pct}
            />
          ))}
        </div>
      )}

      {data && data.retained && (
        <div className="fs-ctx__table-wrap">
          <table className="fs-ctx__table fs-ctx__table--dense">
            <thead>
              <tr>
                <th>{t('Section')}</th>
                <th>{t('Source')}</th>
                <th>{t('Reference')}</th>
                <th>{t('Lanes')}</th>
                <th>{t('Transformation')}</th>
                <th className="fs-ctx__num">{t('Tokens')}</th>
                <th>{t('Why')}</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.itemId} data-testid="context-manifest-item">
                  <td>{item.section}</td>
                  <td>{item.sourceType}</td>
                  <td className="fs-ctx__ref">
                    <code>{item.sourceRef || '—'}</code>
                  </td>
                  <td>{item.lanes.join(', ') || '—'}</td>
                  <td>
                    {item.transformation}
                    {item.generated && (
                      <span className="fs-ctx__flag" data-tone="warning" title={t('A model wrote this text; it is not the source')}>
                        {t('generated')}
                      </span>
                    )}
                  </td>
                  <td className="fs-ctx__num">{item.tokens}</td>
                  <td className="fs-ctx__detail-cell">{item.reason || '—'}</td>
                </tr>
              ))}
              {data.items.length === 0 && (
                <tr>
                  <td colSpan={7} className="fs-ctx__empty-cell">
                    {t('The packet carried nothing: every candidate was left out.')}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {data && (
        <Panel title={t('What was left out, by reason')}>
          {data.omissions.length === 0 ? (
            <p className="fs-muted">{t('Nothing was left out of this packet.')}</p>
          ) : (
            <div className="fs-ctx__shares">
              {data.omissions.map((group) => (
                <Share key={group.reason} label={group.reason} count={String(group.count)} share={group.pct} />
              ))}
            </div>
          )}
        </Panel>
      )}
    </aside>
  );
}

function Packets({
  projectId,
  mode,
  selected,
  onSelect,
}: {
  projectId: string;
  mode: EngineMode | null;
  selected: string;
  onSelect: (id: string) => void;
}) {
  const ledger = useRemote(`packets:${projectId}`, (signal) => loadPackets(projectId, PACKET_PAGE, signal));
  const packets = ledger.data;
  const current = useMemo(() => packets?.find((row) => row.id === selected) ?? null, [packets, selected]);

  if (ledger.loading && !packets) return <Skeleton label={t('Reading the ledger')} count={6} height="44px" />;
  if (!packets) return <Problem error={ledger.error} what={t('The ledger could not be read')} />;

  if (packets.length === 0) {
    return (
      <EmptyState
        icon={Database}
        headingLevel={3}
        title={t('No packets yet')}
        body={
          mode === 'off'
            ? t('The engine is off: nothing is compiled and nothing is recorded. Turn Context Engine on in Settings — shadow mode first, which changes nothing the model sees.')
            : t('The engine is on but has not compiled anything in this scope yet. The first turn it handles will land here.')
        }
      />
    );
  }

  return (
    <div className="fs-ctx__split" data-detail={current ? '' : undefined} data-testid="context-packets">
      <div className="fs-ctx__table-wrap">
        <table className="fs-ctx__table">
          <caption className="fs-ctx__caption">
            {tn(packets.length, '{n} packet, newest first.', '{n} packets, newest first.')}{' '}
            {t('Open one to see what went into it, and what did not.')}
          </caption>
          <thead>
            <tr>
              <th>{t('When')}</th>
              <th>{t('Model')}</th>
              <th>{t('Intent')}</th>
              <th className="fs-ctx__num">{t('Tokens')}</th>
              <th>{t('State')}</th>
              <th className="fs-ctx__num">{t('Left out')}</th>
            </tr>
          </thead>
          <tbody>
            {packets.map((packet) => (
              <tr
                key={packet.id}
                className="fs-ctx__row"
                data-degraded={packet.degraded || undefined}
                aria-current={packet.id === selected ? 'true' : undefined}
                data-testid="context-packet-row"
              >
                <th scope="row" className="fs-ctx__row-head">
                  <button
                    type="button"
                    className="fs-ctx__row-open"
                    onClick={() => onSelect(packet.id === selected ? '' : packet.id)}
                    title={packet.id}
                  >
                    {stamp(packet.createdAt)}
                  </button>
                </th>
                <td>
                  <code>{packet.model || '—'}</code>
                </td>
                <td>
                  {packet.intent || '—'}
                  {packet.phase && <span className="fs-ctx__sub"> · {packet.phase}</span>}
                </td>
                <td className="fs-ctx__num">
                  {packet.tokens}
                  {packet.inputBudget > 0 && <span className="fs-ctx__sub"> · {packet.budgetPct}%</span>}
                </td>
                <td>
                  {packet.degraded ? (
                    <span className="fs-ctx__flag" data-tone="bad" data-testid="context-packet-degraded">
                      <AlertTriangle size={12} aria-hidden="true" /> {t('degraded')}
                    </span>
                  ) : (
                    <span className="fs-ctx__sub">{t('complete')}</span>
                  )}
                </td>
                <td className="fs-ctx__num">{packet.omissions}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {/* Keyed on the packet: opening a second row must not show the first
          one's manifest for a frame while the new one is on the wire. */}
      {current && <ManifestPane key={current.id} packet={current} onClose={() => onSelect('')} />}
    </div>
  );
}

/* ── Blocks: the standing context that is pasted into every prompt ─────── */

function emptyDraft(projectId: string): BlockDraft {
  return {
    type: 'project_rules',
    scope: 'project',
    project_id: projectId,
    title: '',
    content: '',
    priority: 50,
    max_chars: 3000,
    always_loaded: false,
  };
}

function BlockDialog({
  block,
  projectId,
  onClose,
  onSaved,
}: {
  block: Block | null;
  projectId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<BlockDraft>(
    block
      ? {
          type: block.type,
          scope: block.scope,
          project_id: block.projectId,
          title: block.title,
          content: block.content,
          priority: block.priority,
          max_chars: block.maxChars,
          always_loaded: block.alwaysLoaded,
        }
      : emptyDraft(projectId),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const set = (patch: Partial<BlockDraft>) => setDraft((current) => ({ ...current, ...patch }));

  async function save() {
    setSaving(true);
    setError(null);
    try {
      if (block) await updateBlock(block.id, draft, block.revision);
      else await createBlock(draft);
      onSaved();
      onClose();
    } catch (failure) {
      setError(failure);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={block ? t('Edit this block') : t('New block')}
      description={t('A block is standing context: whatever is in it is pasted into the prompts it applies to.')}
      testId="context-block-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Cancel')} onClick={onClose} />
          <Button
            variant="primary"
            size="sm"
            label={block ? t('Save') : t('Create')}
            loading={saving}
            onClick={() => void save()}
            testId="context-block-save"
          />
        </>
      }
    >
      <Problem error={error} what={t('The block could not be saved')} />
      <div className="fs-ctx__form">
        <label className="fs-ctx__label">
          <span>{t('Type')}</span>
          <select className="fs-field" value={draft.type} onChange={(e) => set({ type: e.target.value })}>
            {BLOCK_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
        <label className="fs-ctx__label">
          <span>{t('Scope')}</span>
          <select className="fs-field" value={draft.scope} onChange={(e) => set({ scope: e.target.value })}>
            {BLOCK_SCOPES.map((scope) => (
              <option key={scope} value={scope}>
                {scope}
              </option>
            ))}
          </select>
        </label>
        <label className="fs-ctx__label">
          <span>{t('Project')}</span>
          <input
            className="fs-field"
            value={draft.project_id}
            placeholder={t('every project')}
            onChange={(e) => set({ project_id: e.target.value })}
          />
        </label>
        <label className="fs-ctx__label">
          <span>{t('Priority')}</span>
          <input
            className="fs-field"
            type="number"
            min={0}
            max={100}
            value={draft.priority}
            onChange={(e) => set({ priority: Number(e.target.value) })}
          />
        </label>
        <label className="fs-ctx__label">
          <span>{t('Character cap')}</span>
          <input
            className="fs-field"
            type="number"
            min={1}
            value={draft.max_chars}
            onChange={(e) => set({ max_chars: Number(e.target.value) })}
          />
        </label>
        <label className="fs-ctx__label fs-ctx__label--wide">
          <span>{t('Title')}</span>
          <input className="fs-field" value={draft.title} onChange={(e) => set({ title: e.target.value })} />
        </label>
        <label className="fs-ctx__label fs-ctx__label--wide">
          <span>{t('Content')}</span>
          <textarea
            className="fs-ctx__textarea"
            rows={8}
            value={draft.content}
            onChange={(e) => set({ content: e.target.value })}
            data-testid="context-block-content"
          />
          <span className="fs-ctx__hint">
            {t('Never a credential: a block carrying one is refused by name, and the refusal says which pattern matched.')}
          </span>
        </label>
        <label className="fs-switch fs-ctx__label--wide">
          <input
            type="checkbox"
            checked={draft.always_loaded}
            onChange={(e) => set({ always_loaded: e.target.checked })}
          />
          <span>{t('Always loaded — it enters every prompt in its scope, and competes for the ration')}</span>
        </label>
      </div>
    </Dialog>
  );
}

function AttachDialog({ block, onClose }: { block: Block; onClose: () => void }) {
  const [sessionId, setSessionId] = useState('');
  const [agentId, setAgentId] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState('');

  async function act(kind: 'attach' | 'detach') {
    setBusy(kind);
    setError(null);
    setDone('');
    try {
      const attachments =
        kind === 'attach'
          ? await attachBlock(block.id, { sessionId, agentId, expiresAt })
          : await detachBlock(block.id, { sessionId, agentId });
      setDone(
        attachments.length
          ? tn(attachments.length, '{n} connection now', '{n} connections now')
          : t('Not connected to anything now.'),
      );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy('');
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      title={t('Connect this block')}
      description={t('A connection makes the block load for one session or one agent, whatever its scope says. An expiry is what makes a temporary one safe to make.')}
      testId="context-attach-dialog"
      footer={
        <>
          <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} />
          <Button
            variant="secondary"
            size="sm"
            icon={Unlink}
            label={t('Disconnect')}
            loading={busy === 'detach'}
            onClick={() => void act('detach')}
          />
          <Button
            variant="primary"
            size="sm"
            icon={Link2}
            label={t('Connect')}
            loading={busy === 'attach'}
            onClick={() => void act('attach')}
            testId="context-attach-confirm"
          />
        </>
      }
    >
      <Problem error={error} what={t('The connection could not be changed')} />
      {done && <p className="fs-muted">{done}</p>}
      <div className="fs-ctx__form">
        <label className="fs-ctx__label">
          <span>{t('Session')}</span>
          <input className="fs-field" value={sessionId} onChange={(e) => setSessionId(e.target.value)} />
        </label>
        <label className="fs-ctx__label">
          <span>{t('Agent')}</span>
          <input className="fs-field" value={agentId} onChange={(e) => setAgentId(e.target.value)} />
        </label>
        <label className="fs-ctx__label fs-ctx__label--wide">
          <span>{t('Stops applying at (ISO 8601, optional)')}</span>
          <input
            className="fs-field"
            value={expiresAt}
            placeholder="2026-09-06T18:00:00Z"
            onChange={(e) => setExpiresAt(e.target.value)}
          />
        </label>
      </div>
    </Dialog>
  );
}

function Blocks({ projectId }: { projectId: string }) {
  const list = useRemote(`blocks:${projectId}`, (signal) => loadBlocks(projectId, '', '', signal));
  const audit = useRemote(`audit:${projectId}`, (signal) => loadBlockAudit(projectId, signal));
  const [editing, setEditing] = useState<Block | 'new' | null>(null);
  const [connecting, setConnecting] = useState<Block | null>(null);
  const [removing, setRemoving] = useState<Block | null>(null);
  const [removeError, setRemoveError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const blocks = list.data;
  const report = audit.data;
  const demoted = useMemo(() => new Set(report?.demoted ?? []), [report]);

  function refresh() {
    list.reload();
    audit.reload();
  }

  async function remove() {
    if (!removing) return;
    setBusy(true);
    setRemoveError(null);
    try {
      await deleteBlock(removing.id);
      setRemoving(null);
      refresh();
    } catch (failure) {
      setRemoveError(failure);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fs-ctx__tab" data-testid="context-blocks">
      {report && (
        <Panel title={t('The always-loaded ration')}>
          <p className="fs-ctx__ration" data-over={report.demoted.length > 0 || undefined} data-testid="context-ration">
            <strong>
              {t('{granted} of {total} always-loaded blocks are being served', {
                granted: report.alwaysLoadedGranted,
                total: report.alwaysLoaded,
              })}
            </strong>
            <span className="fs-ctx__sub">
              {' '}
              · {tn(report.alwaysLoadedChars, '{n} character', '{n} characters')}
              {report.maxBlocks > 0 &&
                ` · ${t('the cap is {blocks} blocks or {chars} characters per scope', {
                  blocks: report.maxBlocks,
                  chars: report.maxChars,
                })}`}
            </span>
          </p>
          {report.demoted.length > 0 ? (
            <p className="fs-notice" data-tone="warning" data-testid="context-demoted">
              {tn(
                report.demoted.length,
                '{n} block is over the ration and is not being loaded:',
                '{n} blocks are over the ration and are not being loaded:',
              )}{' '}
              <code>{report.demoted.join(', ')}</code>{' '}
              {t('Lower a priority, or turn one of them off, and the rest come back.')}
            </p>
          ) : (
            <p className="fs-muted">{t('Every always-loaded block fits inside the ration.')}</p>
          )}
          {report.oversized.length > 0 && (
            <p className="fs-notice" data-tone="warning">
              {tn(
                report.oversized.length,
                '{n} block is longer than its own character cap and enters prompts as a prefix:',
                '{n} blocks are longer than their own character cap and enter prompts as a prefix:',
              )}{' '}
              <code>{report.oversized.map((row) => row.id).join(', ')}</code>
            </p>
          )}
          {report.duplicates.length > 0 && (
            <p className="fs-notice" data-tone="warning">
              {t('The same statement is stored more than once:')}{' '}
              <code>{report.duplicates.map((row) => row.ids.join(' = ')).join(' · ')}</code>
            </p>
          )}
          {report.contradictions.length > 0 && (
            <p className="fs-notice" data-tone="warning">
              {t('Two blocks of a type that can only have one answer are loaded at once:')}{' '}
              <code>
                {report.contradictions.map((row) => `${row.type}: ${row.ids.join(', ')}`).join(' · ')}
              </code>
            </p>
          )}
        </Panel>
      )}
      <Problem error={audit.error} what={t('The block audit could not be read')} />

      <div className="fs-ctx__toolbar">
        <span className="fs-spacer" />
        <Button
          variant="primary"
          size="sm"
          icon={Plus}
          label={t('New block')}
          onClick={() => setEditing('new')}
          testId="context-new-block"
        />
      </div>

      {list.loading && !blocks && <Skeleton label={t('Reading the blocks')} count={4} height="44px" />}
      <Problem error={list.error} what={t('The blocks could not be read')} />

      {blocks && blocks.length === 0 && (
        <EmptyState
          icon={Boxes}
          headingLevel={3}
          title={t('No blocks yet')}
          body={t('A block is a short piece of standing context — the project rules, an active goal, a failure worth not repeating. Write one and it starts arriving in the prompts its scope covers.')}
          primaryAction={{ label: t('New block'), icon: Plus, onClick: () => setEditing('new') }}
        />
      )}

      {blocks && blocks.length > 0 && (
        <div className="fs-ctx__table-wrap">
          <table className="fs-ctx__table">
            <thead>
              <tr>
                <th>{t('Block')}</th>
                <th>{t('Scope')}</th>
                <th className="fs-ctx__num">{t('Priority')}</th>
                <th className="fs-ctx__num">{t('Size')}</th>
                <th>{t('Loading')}</th>
                <th>{t('Actions')}</th>
              </tr>
            </thead>
            <tbody>
              {blocks.map((block) => (
                <tr key={block.id} data-testid="context-block-row">
                  <th scope="row" className="fs-ctx__row-head">
                    <span className="fs-ctx__block-title">{block.title || t('(untitled)')}</span>
                    <code className="fs-ctx__sub">{block.type}</code>
                  </th>
                  <td>
                    {block.scope}
                    {block.projectId && <span className="fs-ctx__sub"> · {block.projectId}</span>}
                  </td>
                  <td className="fs-ctx__num">{block.priority}</td>
                  <td className="fs-ctx__num">
                    {block.chars}
                    <span className="fs-ctx__sub"> / {block.maxChars}</span>
                    {block.truncated && (
                      <span className="fs-ctx__flag" data-tone="warning">
                        {t('cut')}
                      </span>
                    )}
                  </td>
                  <td>
                    {block.alwaysLoaded ? (
                      demoted.has(block.id) ? (
                        <span className="fs-ctx__flag" data-tone="bad">
                          <AlertTriangle size={12} aria-hidden="true" /> {t('always — over the ration')}
                        </span>
                      ) : (
                        <span className="fs-ctx__flag" data-tone="good">
                          {t('always')}
                        </span>
                      )
                    ) : (
                      <span className="fs-ctx__sub">{t('by intent or connection')}</span>
                    )}
                  </td>
                  <td className="fs-ctx__actions">
                    <IconButton
                      icon={Pencil}
                      label={t('Edit {name}', { name: block.title || block.id })}
                      size="sm"
                      onClick={() => setEditing(block)}
                    />
                    <IconButton
                      icon={Link2}
                      label={t('Connect {name}', { name: block.title || block.id })}
                      size="sm"
                      onClick={() => setConnecting(block)}
                    />
                    <IconButton
                      icon={Trash2}
                      label={t('Delete {name}', { name: block.title || block.id })}
                      size="sm"
                      onClick={() => setRemoving(block)}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <BlockDialog
          block={editing === 'new' ? null : editing}
          projectId={projectId}
          onClose={() => setEditing(null)}
          onSaved={refresh}
        />
      )}
      {connecting && <AttachDialog block={connecting} onClose={() => setConnecting(null)} />}
      {removing && (
        <Dialog
          open
          onOpenChange={(next) => {
            if (!next) setRemoving(null);
          }}
          title={t('Delete this block?')}
          description={t('The block goes, and so does every connection pointing at it. Nothing else is touched.')}
          testId="context-delete-dialog"
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setRemoving(null)} />
              <Button
                variant="danger-solid"
                size="sm"
                label={t('Delete')}
                loading={busy}
                onClick={() => void remove()}
                testId="context-delete-confirm"
              />
            </>
          }
        >
          <Problem error={removeError} what={t('The block could not be deleted')} />
          <p className="fs-muted">
            <code>{removing.id}</code> · {removing.title || t('(untitled)')}
          </p>
        </Dialog>
      )}
    </div>
  );
}

/* ── Knowledge: what was proved, and what the board is claiming ────────── */

function ExperienceCard({
  experience,
  evidence,
  onFeedback,
}: {
  experience: Experience;
  evidence: string;
  onFeedback: (id: string, kind: string) => void;
}) {
  const verdict = verdictReading(experience.verdict);
  const anti = experience.role === 'anti_pattern';
  return (
    <article
      className="fs-ctx__card"
      data-role={experience.role}
      data-verdict={verdict.tone}
      data-testid="context-experience"
    >
      <header className="fs-ctx__card-head">
        <span className="fs-ctx__role" data-role={experience.role}>
          {anti ? <AlertTriangle size={12} aria-hidden="true" /> : null}
          {anti ? t('Anti-pattern — this is what did not work') : t('Pattern')}
        </span>
        <span className="fs-ctx__verdict" data-tone={verdict.tone} data-testid="context-verdict">
          {t(verdict.label)}
        </span>
        {experience.stale && (
          <span className="fs-ctx__flag" data-tone="warning" title={t('Files it names are not on disk any more')}>
            {t('stale')}
          </span>
        )}
      </header>
      <p className="fs-ctx__claim">{experience.problem || t('(no problem recorded)')}</p>
      {experience.lesson && <p className="fs-prose fs-ctx__lesson">{experience.lesson}</p>}
      {anti && experience.failureModes.length > 0 && (
        <p className="fs-ctx__sub">
          {t('How it failed')}: {experience.failureModes.join(' · ')}
        </p>
      )}
      <p className="fs-ctx__meta">
        {experience.intent && <span>{experience.intent}</span>}
        {experience.technologies.length > 0 && <span>{experience.technologies.join(', ')}</span>}
        <span>
          {experience.verificationRefs.length > 0
            ? t('backed by {refs}', { refs: experience.verificationRefs.join(', ') })
            : t('no verification reference')}
        </span>
        <span>{t('{helpful} helpful · {harmful} harmful', { helpful: experience.helpful, harmful: experience.harmful })}</span>
      </p>
      <div className="fs-ctx__card-actions">
        <Button
          variant="ghost"
          size="sm"
          label={t('It helped')}
          onClick={() => onFeedback(experience.id, 'helpful')}
          title={evidence ? t('Recorded against {ref}', { ref: evidence }) : t('Recorded with no reference')}
        />
        <Button
          variant="ghost"
          size="sm"
          label={t('It hurt')}
          onClick={() => onFeedback(experience.id, 'harmful')}
          title={evidence ? t('Recorded against {ref}', { ref: evidence }) : t('Recorded with no reference')}
        />
      </div>
    </article>
  );
}

function Knowledge({ projectId }: { projectId: string }) {
  const [expQuery, setExpQuery] = useState('');
  const [expTerm, setExpTerm] = useState('');
  const [findQuery, setFindQuery] = useState('');
  const [findTerm, setFindTerm] = useState('');
  const [evidence, setEvidence] = useState('');
  const [feedbackError, setFeedbackError] = useState<unknown>(null);
  const [said, setSaid] = useState('');

  const experiences = useRemote(`exp:${projectId}:${expTerm}`, (signal) =>
    loadExperiences(expTerm, projectId, signal),
  );
  const findings = useRemote(`find:${findTerm}`, (signal) => loadFindings(findTerm, 'open', signal));

  const byId = useMemo(() => {
    const map = new Map<string, Finding>();
    for (const finding of findings.data ?? []) map.set(finding.id, finding);
    return map;
  }, [findings.data]);

  async function feedback(id: string, kind: string) {
    setFeedbackError(null);
    setSaid('');
    try {
      await sendExperienceFeedback(id, kind, evidence);
      setSaid(t('Recorded.'));
      experiences.reload();
    } catch (failure) {
      setFeedbackError(failure);
    }
  }

  return (
    <div className="fs-ctx__halves" data-testid="context-knowledge">
      <section className="fs-ctx__half" aria-labelledby="fs-ctx-experiences">
        <header className="fs-ctx__half-head">
          <h3 id="fs-ctx-experiences">{t('Verified experiences')}</h3>
          <p className="fs-muted">
            {t('What was tried, what it cost, and whether anything proves it. Search never offers an unproved one as an approach.')}
          </p>
        </header>
        <form
          className="fs-ctx__toolbar"
          onSubmit={(event) => {
            event.preventDefault();
            setExpTerm(expQuery.trim());
          }}
        >
          <label className="fs-search fs-ctx__grow">
            <Search size={13} aria-hidden="true" />
            <input
              type="search"
              value={expQuery}
              placeholder={t('A problem, a technology, a symbol…')}
              aria-label={t('Search the experiences')}
              onChange={(event) => setExpQuery(event.target.value)}
            />
          </label>
          <Button variant="secondary" size="sm" label={t('Search')} type="submit" />
        </form>
        <label className="fs-ctx__label fs-ctx__label--wide">
          <span>{t('Evidence for the next thing you mark (a packet id, a changeset, a proof)')}</span>
          <input
            className="fs-field"
            value={evidence}
            onChange={(event) => setEvidence(event.target.value)}
            placeholder={t('helpful according to whom?')}
          />
        </label>
        <Problem error={feedbackError} what={t('The feedback could not be recorded')} />
        {said && <p className="fs-muted">{said}</p>}
        {experiences.loading && !experiences.data && (
          <Skeleton label={t('Reading the experiences')} count={3} height="72px" />
        )}
        <Problem error={experiences.error} what={t('The experiences could not be read')} />
        {experiences.data && experiences.data.length === 0 && (
          <EmptyState
            icon={Gauge}
            headingLevel={3}
            title={t('Nothing matches yet')}
            body={t('An experience arrives when a run ends with a verdict and something that proves it. Until then this stays empty, which is the honest state.')}
          />
        )}
        {experiences.data?.map((experience) => (
          <ExperienceCard
            key={experience.id}
            experience={experience}
            evidence={evidence}
            onFeedback={(id, kind) => void feedback(id, kind)}
          />
        ))}
      </section>

      <section className="fs-ctx__half" aria-labelledby="fs-ctx-findings">
        <header className="fs-ctx__half-head">
          <h3 id="fs-ctx-findings">{t('Findings on the blackboard')}</h3>
          <p className="fs-muted">
            {t('What one worker put up for the others to read. A correction is a new finding pointing at the old one, never an edit.')}
          </p>
        </header>
        <form
          className="fs-ctx__toolbar"
          onSubmit={(event) => {
            event.preventDefault();
            setFindTerm(findQuery.trim());
          }}
        >
          <label className="fs-search fs-ctx__grow">
            <Search size={13} aria-hidden="true" />
            <input
              type="search"
              value={findQuery}
              placeholder={t('A topic, a claim, a tag…')}
              aria-label={t('Search the findings')}
              onChange={(event) => setFindQuery(event.target.value)}
            />
          </label>
          <Button variant="secondary" size="sm" label={t('Search')} type="submit" />
        </form>
        {findings.loading && !findings.data && <Skeleton label={t('Reading the board')} count={3} height="60px" />}
        <Problem error={findings.error} what={t('The board could not be read')} />
        {findings.data && findings.data.length === 0 && (
          <EmptyState
            icon={ScanSearch}
            headingLevel={3}
            title={t('The board is empty')}
            body={t('A finding is a claim with evidence behind it, posted while work is in flight. Nothing is being served right now.')}
          />
        )}
        {findings.data?.map((finding) => {
          const chain = supersedeChain(finding.id, byId);
          return (
            <article className="fs-ctx__card" data-kind={finding.kind} key={finding.id} data-testid="context-finding">
              <header className="fs-ctx__card-head">
                <span className="fs-ctx__role" data-kind={finding.kind}>
                  {finding.kind || 'note'}
                </span>
                <span className="fs-ctx__sub">{finding.topic || finding.scope || '—'}</span>
                <span className="fs-ctx__flag" data-tone={finding.status === 'open' ? 'good' : 'warning'}>
                  {finding.status}
                </span>
              </header>
              <p className="fs-ctx__claim">{finding.claim}</p>
              <p className="fs-ctx__meta">
                <span>{finding.author || t('unknown author')}</span>
                <span>{relativeTime(finding.createdAt)}</span>
                <span>
                  {finding.evidenceRefs.length > 0
                    ? t('evidence: {refs}', { refs: finding.evidenceRefs.join(', ') })
                    : t('no evidence attached')}
                </span>
                {finding.tags.length > 0 && <span>{finding.tags.join(', ')}</span>}
              </p>
              {chain.length > 1 && (
                <p className="fs-ctx__chain" data-testid="context-supersedes">
                  {t('Supersedes')}: <code>{chain.slice(1).join(' → ')}</code>
                </p>
              )}
            </article>
          );
        })}
      </section>
    </div>
  );
}

/* ── Code index ────────────────────────────────────────────────────────── */

function CodeIndex({
  projectId,
  workspace,
  onWorkspace,
}: {
  projectId: string;
  workspace: string;
  onWorkspace: (value: string) => void;
}) {
  const [draft, setDraft] = useState(workspace);
  const [query, setQuery] = useState('');
  const [term, setTerm] = useState('');
  const [full, setFull] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [report, setReport] = useState<RefreshReport | null>(null);
  const [refreshError, setRefreshError] = useState<unknown>(null);
  const [refusal, setRefusal] = useState<RefreshBlock>('');

  const status = useRemote(`cistatus:${workspace}:${projectId}`, (signal) =>
    loadCodeIndexStatus(workspace, projectId, signal),
  );
  const hits = useRemote(`cisearch:${workspace}:${projectId}:${term}`, (signal) =>
    term ? searchCode(term, workspace, projectId, signal) : Promise.resolve<CodeSymbol[]>([]),
  );

  const stale = status.data ? codeIndexStale(status.data) : false;
  const blocked = refreshBlocker(workspace, draft);
  const unapplied = draft.trim() !== workspace;

  // Applying a path answers whatever the last refusal was about.
  useEffect(() => setRefusal(''), [workspace]);

  /** What the button to the right of the field does, and what Enter does. */
  function applyWorkspace() {
    onWorkspace(draft.trim());
  }

  async function refresh() {
    if (blocked) {
      // Neither a result nor a failure: a field nobody applied. The server
      // would answer "scanned 0", which is the same sentence it sends for a
      // repository with nothing in it, and the two need different answers.
      setReport(null);
      setRefreshError(null);
      setRefusal(blocked);
      return;
    }
    setRefusal('');
    setRefreshing(true);
    setRefreshError(null);
    try {
      setReport(await refreshCodeIndex(workspace, projectId, full));
      status.reload();
      if (term) hits.reload();
    } catch (failure) {
      setRefreshError(failure);
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <div className="fs-ctx__tab" data-testid="context-code">
      <form
        className="fs-ctx__toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          applyWorkspace();
        }}
      >
        <label className="fs-search fs-ctx__grow">
          <Database size={13} aria-hidden="true" />
          <input
            value={draft}
            placeholder={t('Workspace path — empty means every workspace in this store')}
            aria-label={t('Workspace')}
            onChange={(event) => setDraft(event.target.value)}
            /* Enter applies, exactly as the button to the right does. The form
               would submit on its own; doing it here as well is what makes the
               behaviour survive a future refactor of the toolbar into
               something that is not a <form>. */
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return;
              event.preventDefault();
              applyWorkspace();
            }}
            data-testid="context-workspace"
          />
        </label>
        <Button variant="secondary" size="sm" label={t('Use this workspace')} type="submit" />
      </form>
      {unapplied && (
        <p className="fs-muted" data-testid="context-workspace-unapplied">
          {t('Press Enter to apply this path.')}
        </p>
      )}

      {status.loading && !status.data && <Skeleton label={t('Reading the index')} count={2} height="56px" />}
      <Problem error={status.error} what={t('The index status could not be read')} />

      {status.data && (
        <div className="fs-ctx__stats">
          <Stat label={t('Symbols')} value={String(status.data.symbols)} hint={t('what a lookup by name can find')} />
          <Stat label={t('Files')} value={String(status.data.files)} hint={Object.keys(status.data.languages).join(', ') || '—'} />
          <Stat label={t('Edges')} value={String(status.data.edges)} hint={t('imports, calls, definitions, tests')} />
          <Stat
            label={t('Last indexed')}
            value={status.data.lastIndexedAt ? relativeTime(status.data.lastIndexedAt) : t('never')}
            hint={
              stale
                ? t('out of date — the index is rebuilt at most every {n} minutes', {
                    n: Math.round(CODE_INDEX_STALE_S / 60),
                  })
                : t('inside the refresh interval')
            }
            tone={stale ? 'warning' : undefined}
            testId="context-index-stale"
          />
        </div>
      )}

      <div className="fs-ctx__toolbar">
        <label className="fs-switch">
          <input type="checkbox" checked={full} onChange={(event) => setFull(event.target.checked)} />
          <span>{t('Full rebuild — open every file, not only the changed ones')}</span>
        </label>
        <span className="fs-spacer" />
        <Button
          variant="secondary"
          size="sm"
          icon={RefreshCw}
          label={t('Refresh the index')}
          loading={refreshing}
          onClick={() => void refresh()}
          testId="context-refresh-index"
        />
      </div>
      <Problem error={refreshError} what={t('The index could not be refreshed')} />
      {refusal && (
        <p className="fs-notice" data-tone="warning" data-testid="context-refresh-blocked">
          {refusal === 'unapplied'
            ? t('You typed a path but have not applied it yet — press Enter, or use this workspace, and then refresh.')
            : t('Name a workspace and apply it first. The index is built one workspace at a time, so there is nothing here to walk yet.')}
        </p>
      )}
      {report && (
        <p className="fs-notice" data-tone={report.truncated ? 'warning' : undefined} data-testid="context-refresh-report">
          {t('Scanned {scanned} · reindexed {reindexed} · removed {removed} · {symbols} symbols in {ms} ms', {
            scanned: report.scanned,
            reindexed: report.reindexed,
            removed: report.removed,
            symbols: report.symbols,
            ms: report.elapsedMs,
          })}
          {report.truncated && ` · ${t('the file budget ran out before the walk did, so this is a partial pass')}`}
        </p>
      )}

      <form
        className="fs-ctx__toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setTerm(query.trim());
        }}
      >
        <label className="fs-search fs-ctx__grow">
          <Search size={13} aria-hidden="true" />
          <input
            type="search"
            value={query}
            placeholder={t('A symbol name, a path, a phrase from a docstring…')}
            aria-label={t('Search the code index')}
            onChange={(event) => setQuery(event.target.value)}
            data-testid="context-code-query"
          />
        </label>
        <Button variant="secondary" size="sm" label={t('Search')} type="submit" />
      </form>

      <Problem error={hits.error} what={t('The search failed')} />
      {term && hits.data && hits.data.length === 0 && !hits.loading && (
        <p className="fs-muted">
          {status.data && status.data.symbols === 0
            ? t('Nothing is indexed here yet. Name a workspace and refresh, and the symbols appear.')
            : t('No symbol matches that. The search is lexical on purpose: an exact identifier is what it answers best.')}
        </p>
      )}
      {hits.data && hits.data.length > 0 && (
        <div className="fs-ctx__table-wrap">
          <table className="fs-ctx__table">
            <thead>
              <tr>
                <th>{t('Symbol')}</th>
                <th>{t('Kind')}</th>
                <th>{t('Signature')}</th>
                <th>{t('Where')}</th>
              </tr>
            </thead>
            <tbody>
              {hits.data.map((symbol) => (
                <tr key={symbol.id} data-testid="context-symbol">
                  <th scope="row" className="fs-ctx__row-head">
                    <code>{symbol.qualname}</code>
                  </th>
                  <td>{symbol.kind}</td>
                  <td className="fs-ctx__ref">
                    <code>{symbol.signature || '—'}</code>
                  </td>
                  <td className="fs-ctx__ref">
                    <code>{symbolRef(symbol)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ── the screen ────────────────────────────────────────────────────────── */

export function ContextScreen() {
  const [params, setParams] = useSearchParams();
  const [mode, setMode] = useState<EngineMode | null>(null);

  const wanted = params.get('t') ?? '';
  const tab: TabId = (TABS.find((entry) => entry.id === wanted)?.id ?? 'overview') as TabId;
  const projectId = params.get('project') ?? '';
  const workspace = params.get('ws') ?? '';
  const selected = params.get('p') ?? '';

  useEffect(() => {
    let alive = true;
    void getSettings().then(
      (settings) => {
        if (alive) setMode(engineMode(settings));
      },
      () => {
        if (alive) setMode(null);
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  /**
   * The open tab lives in the URL, not in storage: it can be linked, sent to
   * whoever is asking why the model knew something, and reopened by the back
   * button. Storage could do none of those and would survive a session that
   * ended for a reason.
   */
  const put = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key === 't') next.delete('p');
    setParams(next, { replace: true });
  };

  return (
    <div className="fs-screen fs-ctx" data-testid="context">
      <header className="fs-screen__head">
        <div>
          <h1 className="fs-screen__title">{t('Context')}</h1>
          <p className="fs-prose fs-ctx__lede">
            {t('One compiler decides what the model is told. This is where you read that decision back: what went into a prompt, what did not, and why.')}
          </p>
        </div>
        {mode !== null && mode !== 'live' && (
          <p className="fs-notice" data-tone="warning" data-testid="context-mode-notice">
            {mode === 'off'
              ? t('The engine is off: nothing here is being compiled for a real turn.')
              : t('The engine is in shadow mode: it compiles a packet each turn and delivers none of it.')}{' '}
            <Link to="/settings">{t('Settings')}</Link>
          </p>
        )}
      </header>

      <div className="fs-tabs" role="tablist" aria-label={t('Context Engine')}>
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={entry.id === tab}
            className="fs-tab"
            data-testid={`context-tab-${entry.id}`}
            onClick={() => put('t', entry.id === 'overview' ? '' : entry.id)}
          >
            {t(entry.label)}
          </button>
        ))}
      </div>

      {tab === 'overview' && <Overview projectId={projectId} workspace={workspace} mode={mode} />}
      {tab === 'packets' && (
        <Packets projectId={projectId} mode={mode} selected={selected} onSelect={(id) => put('p', id)} />
      )}
      {tab === 'blocks' && <Blocks projectId={projectId} />}
      {tab === 'knowledge' && <Knowledge projectId={projectId} />}
      {tab === 'code' && (
        <CodeIndex projectId={projectId} workspace={workspace} onWorkspace={(value) => put('ws', value)} />
      )}
    </div>
  );
}
