import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { t } from '../../i18n';
import { instantiateTemplate, listTemplates, type WorkflowTemplate } from '../../adapters/workflows';
import { addNode, blankDefinition, groupLabel, paletteEntries, type PaletteGroup } from './nodeKinds';

/**
 * Where a plan starts and grows: a blank workflow, the node palette, and the
 * templates the server offers. Adding a node only edits the definition in
 * state; saving, publishing and running are separate steps elsewhere on the
 * screen.
 */

export interface BuildPanelProps {
  definition: Record<string, unknown> | null;
  selectedNodeId: string | null;
  /** `select` is the node to open in the inspector, when one was added. */
  onChange: (definition: Record<string, unknown>, select?: string) => void;
  /** A whole new definition (blank or from a template), replacing the current one. */
  onReplace: (definition: Record<string, unknown>) => void;
}

const GROUPS: PaletteGroup[] = ['model', 'flow', 'effect'];

function TemplatePicker({ onReplace }: { onReplace: (definition: Record<string, unknown>) => void }) {
  const [templates, setTemplates] = useState<WorkflowTemplate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chosen, setChosen] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const ctl = new AbortController();
    listTemplates(ctl.signal)
      .then(setTemplates)
      .catch((e) => { if (!ctl.signal.aborted) setError(e instanceof Error ? e.message : String(e)); });
    return () => ctl.abort();
  }, []);

  const template = templates?.find((x) => x.id === chosen) ?? null;

  function choose(id: string) {
    setChosen(id);
    setError(null);
    const next = templates?.find((x) => x.id === id);
    const initial: Record<string, string> = {};
    for (const p of next?.parameters ?? []) initial[p.name] = p.defaultValue === null ? '' : String(p.defaultValue);
    setValues(initial);
  }

  async function fill() {
    if (!template) return;
    const parameters: Record<string, unknown> = {};
    for (const p of template.parameters) {
      const raw = (values[p.name] ?? '').trim();
      if (raw === '') continue;
      parameters[p.name] = p.kind === 'integer' || p.kind === 'number' ? Number(raw) : raw;
    }
    setBusy(true);
    setError(null);
    try {
      onReplace(await instantiateTemplate(template.id, parameters));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fs-build__templates" data-testid="build-templates">
      <h3 className="fs-build__heading">{t('Templates')}</h3>
      {templates === null && !error && <p className="fs-workflows__empty">{t('Loading templates')}</p>}
      {templates && templates.length === 0 && <p className="fs-workflows__empty">{t('No templates are available.')}</p>}
      {templates && templates.length > 0 && (
        <div className="fs-form">
          <div className="fs-form__field">
            <label className="fs-form__label" htmlFor="build-template-select">{t('Start from a template')}</label>
            <select id="build-template-select" className="fs-form__control" value={chosen} onChange={(e) => choose(e.target.value)} data-testid="build-template-select">
              <option value="">{t('Choose one')}</option>
              {templates.map((x) => <option key={x.id} value={x.id}>{x.title}</option>)}
            </select>
          </div>
          {template && (
            <>
              <p className="fs-form__hint" data-testid="build-template-description">{template.description}</p>
              {template.parameters.map((p) => (
                <div key={p.name} className="fs-form__field">
                  <label className="fs-form__label" htmlFor={`build-param-${p.name}`}>
                    {p.label || p.name}
                    {p.required && <span className="fs-form__req"> {t('(required)')}</span>}
                  </label>
                  <input
                    id={`build-param-${p.name}`} className="fs-form__control" spellCheck={false}
                    type={p.kind === 'integer' || p.kind === 'number' ? 'number' : 'text'}
                    min={p.minimum ?? undefined} max={p.maximum ?? undefined}
                    value={values[p.name] ?? ''} onChange={(e) => setValues((prev) => ({ ...prev, [p.name]: e.target.value }))}
                    data-testid={`build-param-${p.name}`}
                  />
                  {p.help && <p className="fs-form__hint">{p.help}</p>}
                </div>
              ))}
              {template.notes.length > 0 && (
                <ul className="fs-form__hint fs-build__notes">{template.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
              )}
              <Button variant="primary" size="sm" label={t('Fill in template')} onClick={() => void fill()} loading={busy} testId="build-template-fill" />
            </>
          )}
        </div>
      )}
      {error && <p className="fs-workflows__error" role="alert" data-testid="build-template-error">{error}</p>}
    </div>
  );
}

export function BuildPanel({ definition, selectedNodeId, onChange, onReplace }: BuildPanelProps) {
  const entries = paletteEntries();
  return (
    <section className="fs-build" data-testid="build-panel" aria-label={t('Build')}>
      <div className="fs-build__head">
        <h3 className="fs-build__heading">{t('Add a node')}</h3>
        <Button variant="secondary" size="sm" label={t('New blank workflow')} onClick={() => onReplace(blankDefinition())} testId="build-new" />
      </div>
      {!definition && <p className="fs-workflows__empty">{t('Start a blank workflow or a template, then add nodes.')}</p>}
      {definition && GROUPS.map((group) => (
        <div key={group} className="fs-build__group" role="group" aria-label={groupLabel(group)}>
          <span className="fs-build__group-name">{groupLabel(group)}</span>
          <div className="fs-build__entries">
            {entries.filter((e) => e.group === group).map((e) => (
              <button
                key={e.type}
                type="button"
                className="fs-chip"
                title={e.hint}
                data-testid={`palette-${e.type}`}
                onClick={() => {
                  const added = addNode(definition, e.type, selectedNodeId);
                  onChange(added.definition, added.id);
                }}
              >
                {e.label}
              </button>
            ))}
          </div>
        </div>
      ))}
      <TemplatePicker onReplace={onReplace} />
    </section>
  );
}
