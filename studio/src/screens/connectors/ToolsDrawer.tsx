import { useEffect, useState } from 'react';
import { Button, Dialog, Skeleton } from '../../components';
import { Toggle } from '../settings/fields';
import { setMcpDisabledTools } from '../../adapters/integrations';
import { listToolsFor, refreshToolsFor, type Connector, type ConnectorTool } from '../../adapters/connectors';
import { t } from '../../i18n';

/**
 * "View tools" drawer: the same toggle every MCP server already has in
 * Settings → Integrations (`setMcpDisabledTools`, `IntegrationsMore.tsx`'s
 * `McpServerCard`) — this lote does not invent a second way to disable a
 * tool, it reuses the server-scoped one, since a connector's tools ARE that
 * connector's underlying MCP server's tools.
 */
export function ConnectorToolsDrawer({ connector, onClose }: { connector: Connector; onClose: () => void }) {
  const [tools, setTools] = useState<ConnectorTool[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const reload = () =>
    listToolsFor(connector)
      .then((list) => {
        setTools(list);
        setErr(null);
      })
      .catch((e: Error) => {
        setErr(e.message);
        setTools([]);
      });
  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connector.id, connector.server.id]);

  const refresh = async (reconnect: boolean) => {
    setBusy(true);
    setNote(null);
    try {
      const r = await refreshToolsFor(connector, reconnect);
      await reload();
      if (!r.ok) setErr(r.error ?? t('Refresh failed'));
      else if (r.reconnected) setNote(t('Restarted: {n} tools', { n: r.tool_count }));
      else if (r.changed) setNote(t('Updated: {n} tools ({added} added, {removed} removed, {modified} changed)', { n: r.tool_count, added: (r.added ?? []).length, removed: (r.removed ?? []).length, modified: (r.modified ?? []).length }));
      else setNote(t('No changes: {n} tools', { n: r.tool_count }));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const disabledNow = (tools ?? []).filter((x) => x.is_disabled).map((x) => x.name);
  const setDisabled = async (disabled: string[]) => {
    try {
      await setMcpDisabledTools(connector.server.id, disabled);
      setTools((ts) => (ts ?? []).map((x) => ({ ...x, is_disabled: disabled.includes(x.name) })));
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title={t('Tools — {name}', { name: connector.server.name })} testId="connector-tools-dialog">
      <div className="fs-set__row-actions">
        <Button size="sm" variant="secondary" label={t('Refresh tools')} loading={busy} onClick={() => void refresh(false)} testId="connector-tools-refresh" />
        <Button size="sm" variant="ghost" label={t('Restart and refresh')} loading={busy} onClick={() => void refresh(true)} testId="connector-tools-restart" />
      </div>
      {note && <p className="fs-set__help" data-testid="connector-tools-note">{note}</p>}
      {tools === null ? (
        <Skeleton label={t('Loading tools')} count={3} height="32px" />
      ) : tools.length === 0 ? (
        <p className="fs-set__help" data-tone={err ? 'bad' : undefined}>{err ?? t('No tools reported yet — connect the server first.')}</p>
      ) : (
        <>
          <p className="fs-set__help">{tools.length - disabledNow.length}/{tools.length} {t('enabled')}</p>
          <ul className="fs-tools">
            {tools.map((tool) => (
              <li key={tool.name} className="fs-tools__row">
                <span className="fs-tools__text">
                  <strong>{tool.name}</strong>
                  {tool.description && <span className="fs-set__help">{tool.description}</span>}
                </span>
                <Toggle
                  id={`conn-tool-${connector.id}-${tool.name}`}
                  checked={!tool.is_disabled}
                  onChange={(v) => void setDisabled(v ? disabledNow.filter((n) => n !== tool.name) : [...disabledNow, tool.name])}
                />
              </li>
            ))}
          </ul>
        </>
      )}
    </Dialog>
  );
}
