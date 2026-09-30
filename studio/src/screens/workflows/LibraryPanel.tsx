import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, Dialog, Skeleton } from '../../components';
import { t } from '../../i18n';
import {
  deleteSavedWorkflow, getSavedWorkflow, listPublishedTools, listSavedWorkflows, saveWorkflow, updateSavedWorkflow,
  type PublishedTool, type SavedWorkflow,
} from '../../adapters/workflows';
import { JsonField } from './NodeForms';

/**
 * The library: workflows kept under a name, and which of them are published as
 * MCP tools. Publishing is its own switch, off by default and only possible
 * when the definition declares the `inputs` a tool takes; saving never
 * publishes and never runs anything. What a published tool looks like to a
 * client (its name, arguments and the per-run `overrides` it accepts) is shown
 * here from what the server itself lists, not rebuilt on this side.
 */

export interface LibraryPanelProps {
  definition: Record<string, unknown> | null;
  /** Called with the edited definition (its `inputs` schema changed). */
  onDefinitionChange: (definition: Record<string, unknown>) => void;
  /** A saved definition was opened; `name` is its library name. */
  onLoad: (definition: Record<string, unknown>, name: string) => void;
  /** The library name of the definition on screen, when it came from there. */
  savedName: string | null;
  onSaved: (name: string) => void;
}

const SERVER_NAME = 'faustus-workflows';

/** What a client's MCP config gets. Paths and the token are placeholders: the
 *  person fills in where Faustus lives and an API token with the
 *  `agents:dispatch` scope. */
export function clientConfigSnippet(): string {
  return JSON.stringify({
    mcpServers: {
      [SERVER_NAME]: {
        command: '<path to the Faustus Python>',
        args: ['<Faustus folder>/mcp_servers/workflows_server.py'],
        env: { FAUSTUS_URL: window.location.origin, FAUSTUS_API_TOKEN: '<API token with the agents:dispatch scope>' },
      },
    },
  }, null, 2);
}

export function LibraryPanel({ definition, onDefinitionChange, onLoad, savedName, onSaved }: LibraryPanelProps) {
  const [items, setItems] = useState<SavedWorkflow[] | null>(null);
  const [tools, setTools] = useState<PublishedTool[]>([]);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [list, published] = await Promise.all([listSavedWorkflows(), listPublishedTools()]);
      setItems(list);
      setTools(published);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setItems((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  const definitionId = typeof definition?.id === 'string' ? definition.id : '';
  useEffect(() => { setName(savedName || definitionId); }, [savedName, definitionId]);

  async function run(label: string, action: () => Promise<void>) {
    setBusy(label);
    setError(null);
    setNotice(null);
    try {
      await action();
      await reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const save = () => run('save', async () => {
    if (!definition) return;
    const saved = await saveWorkflow(definition, { name: name.trim() || undefined });
    onSaved(saved.name);
    setNotice(t('Saved as {name}. It is not published.', { name: saved.name }));
  });

  const toolOf = useMemo(() => {
    const byName = new Map(tools.map((x) => [x.name, x]));
    return (row: SavedWorkflow) => byName.get(row.tool) ?? null;
  }, [tools]);

  const declaresInputs = !!definition && typeof definition.inputs === 'object' && definition.inputs !== null && Object.keys(definition.inputs as object).length > 0;

  return (
    <section className="fs-library" data-testid="library-panel" aria-label={t('Library')}>
      <h3 className="fs-build__heading">{t('Library and tools')}</h3>

      {definition && (
        <div className="fs-form">
          <JsonField
            label={t('Inputs a run takes (JSON schema)')}
            value={definition.inputs}
            rows={6}
            testId="library-inputs"
            hint={t('What a caller must pass. A workflow needs this before it can be published as a tool.')}
            onChange={(v) => {
              const next = { ...definition };
              if (v === undefined) delete next.inputs;
              else next.inputs = v;
              onDefinitionChange(next);
            }}
          />
          <div className="fs-form__field">
            <label className="fs-form__label" htmlFor="library-name">{t('Name in the library')}</label>
            <input id="library-name" className="fs-form__control" value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} data-testid="library-name" />
          </div>
          <div className="fs-library__actions">
            <Button variant="primary" size="sm" label={t('Save to library')} onClick={() => void save()} loading={busy === 'save'} testId="library-save" />
            <span className="fs-form__hint">{declaresInputs ? t('Declares inputs: it can be published.') : t('No inputs declared: it cannot be published yet.')}</span>
          </div>
        </div>
      )}

      {error && <p className="fs-workflows__error" role="alert" data-testid="library-error">{error}</p>}
      {notice && <p className="fs-workflows__notice" data-testid="library-notice">{notice}</p>}

      {items === null && <Skeleton label={t('Loading the library')} count={2} height="36px" />}
      {items && items.length === 0 && <p className="fs-workflows__empty" data-testid="library-empty">{t('Nothing saved yet.')}</p>}
      {items && items.length > 0 && (
        <ul className="fs-library__list" data-testid="library-list">
          {items.map((row) => {
            const tool = toolOf(row);
            const overrides = tool && typeof tool.inputSchema.properties === 'object' ? (tool.inputSchema.properties as Record<string, unknown>).overrides : undefined;
            return (
              <li key={row.name} className="fs-library__row" data-testid={`library-row-${row.name}`}>
                <div className="fs-library__line">
                  <strong>{row.title || row.name}</strong>
                  <code>{row.name}</code>
                  <span className="fs-form__hint">{t('{n} nodes', { n: row.nodes })}</span>
                  <span className="fs-library__state" data-on={row.enabled || undefined} data-testid={`library-state-${row.name}`}>
                    {row.enabled ? t('Published') : t('Not published')}
                  </span>
                </div>
                <div className="fs-library__controls">
                  <Button variant="secondary" size="sm" label={t('Open')} testId={`library-open-${row.name}`}
                    onClick={() => void run('open', async () => {
                      const full = await getSavedWorkflow(row.name);
                      onLoad(full.definition, row.name);
                    })} />
                  <label className="fs-form__inline">
                    <input type="checkbox" checked={row.enabled} disabled={!row.publishable && !row.enabled || busy !== null} data-testid={`library-publish-${row.name}`}
                      onChange={() => void run('publish', async () => { await updateSavedWorkflow(row.name, { enabled: !row.enabled }); })} />
                    {t('Publish as a tool')}
                  </label>
                  <label className="fs-form__inline">
                    <input type="checkbox" checked={row.allowOverrides} disabled={busy !== null} data-testid={`library-overrides-${row.name}`}
                      onChange={() => void run('overrides', async () => { await updateSavedWorkflow(row.name, { allowOverrides: !row.allowOverrides }); })} />
                    {t('Allow per-run overrides')}
                  </label>
                  <Button variant="ghost" size="sm" label={open === row.name ? t('Hide tool details') : t('Tool details')} testId={`library-details-${row.name}`}
                    onClick={() => setOpen(open === row.name ? null : row.name)} />
                  <Button variant="danger" size="sm" label={t('Delete')} testId={`library-delete-${row.name}`} onClick={() => setDeleting(row.name)} />
                </div>
                {!row.publishable && !row.enabled && <p className="fs-form__hint">{t('Declare an inputs schema and save again to publish this workflow.')}</p>}
                {open === row.name && (
                  <div className="fs-library__details" data-testid={`library-tool-${row.name}`}>
                    <dl className="fs-inspector__facts">
                      <div><dt>{t('Tool name')}</dt><dd><code data-testid={`library-tool-name-${row.name}`}>{row.tool}</code></dd></div>
                      <div><dt>{t('Description')}</dt><dd>{tool?.description || row.description || '—'}</dd></div>
                    </dl>
                    {tool ? (
                      <>
                        <h4>{t('Arguments (input schema)')}</h4>
                        <pre className="fs-library__pre" data-testid={`library-tool-schema-${row.name}`}>{JSON.stringify(tool.inputSchema, null, 2)}</pre>
                        <h4>{t('Per-run overrides')}</h4>
                        {overrides
                          ? <pre className="fs-library__pre" data-testid={`library-tool-overrides-${row.name}`}>{JSON.stringify(overrides, null, 2)}</pre>
                          : <p className="fs-form__hint">{t('Overrides are off for this workflow.')}</p>}
                        <h4>{t('Client configuration')}</h4>
                        <pre className="fs-library__pre" data-testid="library-client-config">{clientConfigSnippet()}</pre>
                      </>
                    ) : (
                      <p className="fs-form__hint">{t('Not published, so no client sees it as a tool.')}</p>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <Dialog
        open={deleting !== null}
        onOpenChange={(o) => { if (!o) setDeleting(null); }}
        title={t('Delete this saved workflow?')}
        description={t('It is removed from the library, and from the tools it published. Runs already started are not touched.')}
        testId="library-delete-confirm"
        footer={
          <>
            <Button variant="ghost" label={t('Cancel')} onClick={() => setDeleting(null)} testId="library-delete-cancel" />
            <Button variant="danger" label={t('Delete')} testId="library-delete-go"
              onClick={() => { const target = deleting; setDeleting(null); if (target) void run('delete', () => deleteSavedWorkflow(target)); }} />
          </>
        }
      />
    </section>
  );
}
