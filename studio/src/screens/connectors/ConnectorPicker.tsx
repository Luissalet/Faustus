import { Info, Plug } from 'lucide-react';
import { useEffect, useState } from 'react';
import { t } from '../../i18n';
import { listConnectors, type Connector } from '../../adapters/connectors';

/**
 * CONTRATO_CONECTORES F3: "Conectores de esta conversación/proyecto/tarea".
 *
 * A reusable multi-select over every connector `/api/connectors` lists
 * (Hoard presets and every other MCP server, unified — F2's `connector_ids`
 * is a list of `McpServer.id`, nothing narrower). `value: null` means
 * "inherit" — the project's selection, or every enabled connector when
 * there is no project — and is the only state that shows the inherited
 * `effective` set instead of a plain count.
 *
 * This component only decides WHAT is selected; it never calls the save
 * route itself; each screen it is mounted in (session settings, project,
 * task form) owns its own persistence call, the same way three different
 * screens can reuse a `<select>` without agreeing on where it saves to.
 */

export interface ConnectorPickerProps {
  value: string[] | null;
  onChange: (next: string[] | null) => void;
  /** What "inherit" resolves to here, if known — e.g. the project's own
   *  list, or every enabled connector when there is no project. Shown next
   *  to the "Inherit" choice so picking it is not a leap of faith. */
  effective?: string[];
  /** Where the effective list actually came from, per F2's GET contract. */
  source?: 'session' | 'project' | 'all';
  /** Label for the "inherit" option — differs by where this is mounted:
   *  "Inherit from project" in a session, "All enabled" in a project. */
  inheritLabel?: string;
  disabled?: boolean;
  /** F2.4: when the model/endpoint this picker applies to cannot use tools
   *  at all, connectors would never be reached — shown as an inline notice,
   *  never used to change the model. */
  toolSupport?: { supported: boolean | 'unknown'; reason: string } | null;
}

export function ConnectorPicker({ value, onChange, effective, source, inheritLabel, disabled, toolSupport }: ConnectorPickerProps) {
  const [options, setOptions] = useState<Connector[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    listConnectors()
      .then((list) => {
        if (live) setOptions(list);
      })
      .catch(() => {
        if (live) {
          setOptions([]);
          setFailed(true);
        }
      });
    return () => {
      live = false;
    };
  }, []);

  const inheriting = value === null;
  const selected = new Set(value ?? []);

  const toggle = (id: string) => {
    if (disabled) return;
    if (inheriting) {
      // Leaving "inherit" starts from what it currently resolves to, so
      // switching to an explicit list never silently drops everything that
      // was working a moment ago.
      const start = new Set(effective ?? []);
      if (start.has(id)) start.delete(id);
      else start.add(id);
      onChange([...start]);
      return;
    }
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onChange([...next]);
  };

  return (
    <div className="fs-conn-pick" data-testid="connector-picker">
      <div className="fs-conn-pick__head">
        <span className="fs-set__label">
          <Plug size={13} aria-hidden="true" /> {t('Connectors')}
        </span>
        {source && (
          <span className="fs-set__help" data-testid="connector-picker-source">
            {source === 'session' && t('Chosen for this conversation.')}
            {source === 'project' && t('Inherited from the project.')}
            {source === 'all' && t('Every enabled connector.')}
          </span>
        )}
      </div>

      {toolSupport && toolSupport.supported === false && (
        <p className="fs-notice" data-tone="warning" role="alert" data-testid="connector-picker-tool-support">
          <Info size={13} aria-hidden="true" /> {t('This model does not support tools; connectors will not be used.')}
          {toolSupport.reason ? ` (${toolSupport.reason})` : ''}
        </p>
      )}

      {failed ? (
        <p className="fs-set__help" data-tone="bad">{t('Could not load the connector list.')}</p>
      ) : options === null ? (
        <p className="fs-set__help">{t('Loading…')}</p>
      ) : (
        <div className="fs-conn-pick__list" role="group" aria-label={t('Connectors')}>
          <button
            type="button"
            className="fs-chip"
            data-on={inheriting || undefined}
            disabled={disabled}
            onClick={() => onChange(null)}
            data-testid="connector-picker-inherit"
          >
            {inheritLabel ?? t('Inherit')}
          </button>
          {options.map((c) => {
            const on = inheriting ? (effective ?? []).includes(c.server.id) : selected.has(c.server.id);
            return (
              <button
                key={c.server.id}
                type="button"
                className="fs-chip"
                data-on={on || undefined}
                disabled={disabled}
                title={c.preset?.purpose ?? c.server.name}
                onClick={() => toggle(c.server.id)}
                data-testid={`connector-picker-item-${c.server.id}`}
              >
                {c.server.name || c.preset?.name || c.server.id}
              </button>
            );
          })}
          {options.length === 0 && <span className="fs-set__help">{t('No connectors configured yet.')}</span>}
        </div>
      )}
    </div>
  );
}
