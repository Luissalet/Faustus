import { Search, ShieldAlert, Star, Wrench } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Button, EmptyState, Skeleton } from '../../components';
import { listTools, setDisabledTools, TOOL_META, type ToolFlag } from '../../adapters/account';
import {
  dryRunTool,
  getToolDescriptor,
  listToolCatalog,
  loadFavoriteTools,
  saveFavoriteTools,
  type ArgumentIssue,
  type CatalogEntry,
  type CatalogTool,
  type DryRunResult,
} from '../../adapters/tools';
import { t } from '../../i18n';
import { Toggle } from './fields';

/**
 * Built-in tools: which ones the agent may use, by family. The route
 * replaces the whole disabled list, so every change re-reads first and
 * posts a list rebuilt from fresh state, as the previous interface did.
 */
export function ToolsSection({ say }: { say: (t: string) => void }) {
  const [tools, setTools] = useState<ToolFlag[] | null>(null);
  const [denied, setDenied] = useState(false);
  const reload = () =>
    listTools()
      .then(setTools)
      .catch(() => {
        setDenied(true);
        setTools([]);
      });
  useEffect(() => {
    void reload();
  }, []);

  const groups = useMemo(() => {
    const by = new Map<string, ToolFlag[]>();
    for (const tool of tools ?? []) {
      const cat = TOOL_META[tool.id]?.cat ?? 'Other';
      by.set(cat, [...(by.get(cat) ?? []), tool]);
    }
    return [...by.entries()];
  }, [tools]);

  const apply = async (changes: { id: string; enabled: boolean }[]) => {
    try {
      const latest = await listTools();
      const state = new Map(latest.map((x) => [x.id, x.enabled]));
      for (const c of changes) state.set(c.id, c.enabled);
      await setDisabledTools([...state.entries()].filter(([, on]) => !on).map(([id]) => id));
      setTools([...state.entries()].map(([id, enabled]) => ({ id, enabled })));
    } catch {
      say(t('Could not update the tools.'));
      void reload();
    }
  };

  if (denied) return <EmptyState icon={Wrench} title={t('Administrators only')} body={t('This account cannot change the tools.')} />;

  return (
    <>
      <section className="fs-set__section" aria-labelledby="fs-set-tools">
        <header className="fs-set__section-head">
          <div>
            <h2 id="fs-set-tools" className="fs-set__title">{t('Tools')}</h2>
            <p className="fs-prose">{t('What the agent may use. Off here is off for every model and every chat; MCP servers live under Integrations.')}</p>
          </div>
        </header>
        {tools === null ? (
          <Skeleton label={t('Loading')} count={4} height="44px" />
        ) : tools.length === 0 ? (
          <p className="fs-set__help">{t('No tools.')}</p>
        ) : (
          groups.map(([cat, items]) => {
            const on = items.filter((x) => x.enabled).length;
            return (
              <div key={cat} className="fs-set__card">
                <h3 className="fs-set__card-title fs-tools__cat">
                  <span>
                    {t(cat)} <span className="fs-set__help">{on}/{items.length}</span>
                  </span>
                  <Toggle id={`tools-${cat}`} checked={on === items.length} onChange={(v) => void apply(items.map((x) => ({ id: x.id, enabled: v })))} label={t('all')} />
                </h3>
                <ul className="fs-tools">
                  {items.map((tool) => {
                    const meta = TOOL_META[tool.id];
                    return (
                      <li key={tool.id} className="fs-tools__row">
                        <span className="fs-tools__text">
                          <strong>{meta ? t(meta.name) : tool.id}</strong>
                          <span className="fs-set__help">
                            {meta ? t(meta.desc) : ''} <code className="fs-tools__id">{tool.id}</code>
                          </span>
                        </span>
                        <Toggle id={`tool-${tool.id}`} checked={tool.enabled} onChange={(v) => void apply([{ id: tool.id, enabled: v }])} />
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })
        )}
      </section>
      <ToolCatalogPanel say={say} />
    </>
  );
}

/* ── Catalog: one descriptor per tool (native, fence or live MCP), with a
 * safe "try arguments" dry-run (TOOL-01, TOOL-03 partial). Separate section,
 * separate data source (`src/tool_registry.py` via `/api/tools/catalog`) —
 * the enable/disable list above is untouched. ── */

const EXECUTOR_FILTERS = ['', 'native', 'fence', 'mcp'] as const;
type ExecutorFilter = (typeof EXECUTOR_FILTERS)[number];

const EXECUTOR_LABEL: Record<Exclude<ExecutorFilter, ''>, string> = {
  native: 'Native',
  fence: 'Fence',
  mcp: 'MCP',
};

const EFFECT_LABEL: Record<string, string> = {
  read: 'Read',
  write: 'Write',
  execute: 'Execute',
  external: 'External',
  sensitive: 'Sensitive',
  control: 'Control',
};

function matchesExecutor(tool: CatalogTool, filter: ExecutorFilter): boolean {
  if (!filter) return true;
  if (filter === 'mcp') return tool.executor.startsWith('mcp:');
  return tool.executor === filter;
}

function ToolCatalogPanel({ say }: { say: (t: string) => void }) {
  const [rows, setRows] = useState<CatalogTool[] | null>(null);
  const [denied, setDenied] = useState(false);
  const [query, setQuery] = useState('');
  const [effectFilter, setEffectFilter] = useState('');
  const [executorFilter, setExecutorFilter] = useState<ExecutorFilter>('');
  const [favorites, setFavorites] = useState<Set<string>>(() => loadFavoriteTools());
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    listToolCatalog()
      .then((d) => setRows(d.tools))
      .catch(() => {
        setDenied(true);
        setRows([]);
      });
  }, []);

  const effects = useMemo(() => [...new Set((rows ?? []).map((r) => r.effect_class))].sort(), [rows]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (rows ?? [])
      .filter((r) => !needle || r.name.toLowerCase().includes(needle) || r.description.toLowerCase().includes(needle))
      .filter((r) => !effectFilter || r.effect_class === effectFilter)
      .filter((r) => matchesExecutor(r, executorFilter))
      .sort((a, b) => {
        const fav = Number(favorites.has(b.name)) - Number(favorites.has(a.name));
        return fav || a.name.localeCompare(b.name);
      });
  }, [rows, query, effectFilter, executorFilter, favorites]);

  const toggleFavorite = (name: string) => {
    setFavorites((cur) => {
      const next = new Set(cur);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      saveFavoriteTools(next);
      return next;
    });
  };

  if (denied) {
    return (
      <EmptyState
        icon={ShieldAlert}
        title={t('Administrators only')}
        body={t('This account cannot see the tool catalog.')}
      />
    );
  }

  return (
    <section className="fs-set__section" aria-labelledby="fs-set-tool-catalog">
      <header className="fs-set__section-head">
        <div>
          <h2 id="fs-set-tool-catalog" className="fs-set__title">{t('Tool catalog')}</h2>
          <p className="fs-prose">{t('Every tool the agent can be offered, with its schema, effects, timeout and MCP status in one place. Try arguments below without running anything.')}</p>
        </div>
      </header>

      <div className="fs-set__card">
        <div className="fs-set__search">
          <Search size={14} aria-hidden="true" />
          <input
            type="search"
            placeholder={t('Search tools…')}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label={t('Search')}
            data-testid="tool-catalog-search"
          />
        </div>
        <div className="fs-intg__kinds">
          <button type="button" className="fs-chip" data-on={!effectFilter || undefined} onClick={() => setEffectFilter('')}>
            {t('All effects')}
          </button>
          {effects.map((effect) => (
            <button key={effect} type="button" className="fs-chip" data-on={effectFilter === effect || undefined} onClick={() => setEffectFilter(effectFilter === effect ? '' : effect)}>
              {t(EFFECT_LABEL[effect] ?? effect)}
            </button>
          ))}
        </div>
        <div className="fs-intg__kinds">
          {EXECUTOR_FILTERS.map((ex) => (
            <button key={ex || 'all'} type="button" className="fs-chip" data-on={executorFilter === ex || undefined} onClick={() => setExecutorFilter(ex)}>
              {ex ? t(EXECUTOR_LABEL[ex]) : t('All executors')}
            </button>
          ))}
        </div>

        {rows === null ? (
          <Skeleton label={t('Loading')} count={5} height="52px" />
        ) : filtered.length === 0 ? (
          <p className="fs-set__help">{t('No tools match.')}</p>
        ) : (
          <ul className="fs-tools">
            {filtered.map((tool) => (
              <li key={tool.name} className="fs-tools__row">
                <button
                  type="button"
                  className="fs-intg__main"
                  onClick={() => setSelected(selected === tool.name ? null : tool.name)}
                  data-testid={`tool-catalog-row-${tool.name}`}
                >
                  <span className="fs-intg__kind">{t(EFFECT_LABEL[tool.effect_class] ?? tool.effect_class)}</span>
                  <span className="fs-tools__text">
                    <strong>{tool.name} <span className="fs-set__help">v{tool.version}</span></strong>
                    <span className="fs-set__help">
                      {tool.description || t('No description.')} <code className="fs-tools__id">{tool.executor}</code>
                    </span>
                  </span>
                  {tool.mcp && (
                    <span className="fs-set__help" data-tone={tool.mcp.status === 'connected' ? 'ok' : tool.mcp.status === 'error' ? 'bad' : undefined}>
                      <span className="fs-intg__dot" data-on={tool.mcp.status === 'connected' || undefined} /> {tool.mcp.status}
                    </span>
                  )}
                </button>
                <IconStar on={favorites.has(tool.name)} onClick={() => toggleFavorite(tool.name)} label={t('Favorite')} />
              </li>
            ))}
          </ul>
        )}
      </div>

      {selected && <ToolTryPanel name={selected} onClose={() => setSelected(null)} say={say} />}
    </section>
  );
}

function IconStar({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button type="button" className="fs-chip" data-on={on || undefined} onClick={onClick} aria-pressed={on} title={label}>
      <Star size={14} aria-hidden="true" fill={on ? 'currentColor' : 'none'} />
    </button>
  );
}

/** One property's raw form value — kept as a string (or 'true'/'false' for a
 * boolean) until "Try arguments" coerces it, so a half-typed number never
 * fights the input while the user is still typing it. */
type FieldDraft = Record<string, string>;

function coerceArgument(schema: Record<string, unknown>, raw: string): { present: boolean; value: unknown } {
  if (raw === '') return { present: false, value: undefined };
  const types = schema.type;
  const type = Array.isArray(types) ? types[0] : types;
  if (type === 'boolean') return { present: true, value: raw === 'true' };
  if (type === 'integer' || type === 'number') {
    const n = Number(raw);
    return { present: true, value: Number.isNaN(n) ? raw : n };
  }
  if (type === 'array' || type === 'object') {
    try {
      return { present: true, value: JSON.parse(raw) };
    } catch {
      return { present: true, value: raw }; // let the backend's own validator name the type mismatch
    }
  }
  return { present: true, value: raw };
}

function errorsFor(field: string, errors: ArgumentIssue[]): ArgumentIssue[] {
  return errors.filter((e) => e.field === field);
}

function ToolTryPanel({ name, onClose, say }: { name: string; onClose: () => void; say: (t: string) => void }) {
  const [entry, setEntry] = useState<CatalogEntry | null>(null);
  const [draft, setDraft] = useState<FieldDraft>({});
  const [rawJson, setRawJson] = useState('{}');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DryRunResult | null>(null);
  const [loadError, setLoadError] = useState('');

  useEffect(() => {
    setEntry(null);
    setResult(null);
    setDraft({});
    getToolDescriptor(name)
      .then(setEntry)
      .catch(() => setLoadError(t('Could not load this tool.')));
  }, [name]);

  const properties = (entry?.tool.input_schema.properties ?? {}) as Record<string, Record<string, unknown>>;
  const required = new Set((entry?.tool.input_schema.required as string[] | undefined) ?? []);
  const propertyNames = Object.keys(properties);
  const hasSchema = propertyNames.length > 0;

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      let args: Record<string, unknown>;
      if (hasSchema) {
        args = {};
        for (const key of propertyNames) {
          const { present, value } = coerceArgument(properties[key] ?? {}, draft[key] ?? '');
          if (present) args[key] = value;
        }
      } else {
        args = JSON.parse(rawJson || '{}');
      }
      setResult(await dryRunTool(name, args));
    } catch {
      say(t('Could not run the dry-run check.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fs-set__card" data-testid="tool-try-panel">
      <h3 className="fs-set__card-title fs-tools__cat">
        <span>{t('Try arguments')} — <code className="fs-tools__id">{name}</code></span>
        <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} />
      </h3>
      <p className="fs-set__help">{t('Checks your arguments against the schema and shows what would be rejected or auto-corrected. Nothing runs.')}</p>

      {loadError ? (
        <p className="fs-set__help" data-tone="bad">{loadError}</p>
      ) : !entry ? (
        <Skeleton label={t('Loading')} count={3} height="36px" />
      ) : (
        <>
          {hasSchema ? (
            <div className="fs-tools">
              {propertyNames.map((key) => {
                const prop = properties[key];
                const fieldErrors = errorsFor(key, result?.errors ?? []);
                const repaired = result?.repairs.find((r) => r.field === key);
                return (
                  <div key={key} className="fs-tools__row">
                    <span className="fs-tools__text" style={{ flex: 1 }}>
                      <strong>
                        {key}
                        {required.has(key) ? ' *' : ''} <span className="fs-set__help">{String(prop.type ?? '')}</span>
                      </strong>
                      {prop.type === 'boolean' ? (
                        <select className="fs-field" value={draft[key] ?? ''} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}>
                          <option value="">{t('unset')}</option>
                          <option value="true">true</option>
                          <option value="false">false</option>
                        </select>
                      ) : Array.isArray(prop.enum) ? (
                        <select className="fs-field" value={draft[key] ?? ''} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}>
                          <option value="">{t('unset')}</option>
                          {(prop.enum as unknown[]).map((v) => (
                            <option key={String(v)} value={String(v)}>{String(v)}</option>
                          ))}
                        </select>
                      ) : (
                        <input
                          className="fs-field"
                          type="text"
                          value={draft[key] ?? ''}
                          placeholder={String(prop.description ?? '')}
                          onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                        />
                      )}
                      {fieldErrors.map((err, i) => (
                        <span key={i} className="fs-set__help" data-tone="bad">{err.detail}</span>
                      ))}
                      {repaired && (
                        <span className="fs-set__help" data-tone="ok">
                          {t('Auto-corrected to')} {JSON.stringify(repaired.to)}
                        </span>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <>
              <p className="fs-set__help">{t('This tool publishes no JSON schema — enter raw JSON arguments.')}</p>
              <textarea
                className="fs-field fs-set__pre"
                rows={4}
                value={rawJson}
                onChange={(e) => setRawJson(e.target.value)}
                aria-label={t('Arguments (JSON)')}
              />
            </>
          )}

          <Button variant="primary" label={t('Try arguments')} onClick={() => void run()} loading={busy} disabled={busy} testId="tool-try-run" />

          {result && (
            <p className="fs-set__help" data-tone={result.ok ? 'ok' : 'bad'} data-testid="tool-try-result">
              {result.ok ? t('Valid arguments.') : t('Rejected by the schema.')}
              {!result.schema_available && ` (${t('no schema to check')})`}
            </p>
          )}
        </>
      )}
    </div>
  );
}
