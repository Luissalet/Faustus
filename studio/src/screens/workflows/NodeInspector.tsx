import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { t } from '../../i18n';
import type { LintFinding } from '../../adapters/topology';
import { ConfigForm, FORM_TYPES, type Config } from './NodeForms';
import { declaredLabels, type RawNode } from './nodeKinds';

/**
 * W2-E (CMP-07): the contract of one node — entry/exit (`needs`), the
 * tool/model it names, and its `config`. The model-driven types (agent,
 * classify, extract, guard, loop) get a form; every type keeps the raw JSON
 * editor, which shows the same object, so nothing a form cannot express is
 * out of reach. Changing a node here never runs anything by itself:
 * `WorkflowsScreen` re-runs `workflowPreflight` (which re-runs
 * `agent_profile_lint.lint_workflow`) against the edited definition, and this
 * panel shows whatever findings now name this node.
 */

export interface InspectorNode {
  id: string;
  type: string;
  title: string;
  needs: string[];
  config: Record<string, unknown>;
  branch: Record<string, string[]>;
}

/** What the inspector changes besides `config`. */
export interface NodePatch {
  title: string;
  needs: string[];
  branch: Record<string, string[]>;
}

export interface NodeInspectorProps {
  node: InspectorNode | null;
  /** Every node of the definition: the choices for `needs`, branch labels and a loop's body. */
  nodes?: RawNode[];
  profiles?: string[];
  warnings: LintFinding[];
  onClose: () => void;
  onApplyConfig: (nodeId: string, config: Record<string, unknown>, patch?: NodePatch) => void;
  onRemove?: (nodeId: string) => void;
  busy?: boolean;
}

/** `agent_profile_lint`'s `subject` for a workflow finding is `"node:<id>"`
 *  (most rules) or `"workflow:<id1>->...->id"` (a cycle) — match both. */
function subjectMatchesNode(subject: string, nodeId: string): boolean {
  return subject === `node:${nodeId}` || subject.split('->').includes(nodeId);
}

export function NodeInspector({ node, nodes = [], profiles = [], warnings, onClose, onApplyConfig, onRemove, busy }: NodeInspectorProps) {
  const [config, setConfig] = useState<Config>({});
  const [raw, setRaw] = useState('');
  const [title, setTitle] = useState('');
  const [needs, setNeeds] = useState<string[]>([]);
  const [branch, setBranch] = useState<Record<string, string[]>>({});
  const [parseError, setParseError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setConfig(node?.config ?? {});
    setRaw(node ? JSON.stringify(node.config, null, 2) : '');
    setTitle(node?.title ?? '');
    setNeeds(node?.needs ?? []);
    setBranch(node?.branch ?? {});
    setParseError(null);
    setDirty(false);
  }, [node?.id]);

  if (!node) {
    return (
      <aside className="fs-inspector fs-inspector--empty" data-testid="node-inspector-empty">
        <p>{t('Select a node in the plan to inspect its contract.')}</p>
      </aside>
    );
  }

  const current = node; // narrowed non-null once, so the closures below keep that type
  const relevant = warnings.filter((w) => subjectMatchesNode(w.subject, current.id));
  const self: RawNode = { id: current.id, type: current.type, title, needs, config };
  const others = nodes.filter((n) => n.id !== current.id);
  const model = typeof config.model === 'string' ? config.model : '';
  const skill = typeof config.skill === 'string' ? config.skill : '';
  const backend = typeof config.backend === 'string' ? config.backend : '';
  const hasForm = (FORM_TYPES as readonly string[]).includes(current.type);

  function editConfig(next: Config) {
    setConfig(next);
    setRaw(JSON.stringify(next, null, 2));
    setParseError(null);
    setDirty(true);
  }

  function editRaw(text: string) {
    setRaw(text);
    setDirty(true);
    try {
      const parsed = JSON.parse(text || '{}');
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setParseError(t('config must be a JSON object.'));
        return;
      }
      setParseError(null);
      setConfig(parsed as Config);
    } catch {
      setParseError(t('config must be valid JSON.'));
    }
  }

  function toggleNeed(id: string) {
    const next = needs.includes(id) ? needs.filter((n) => n !== id) : [...needs, id];
    setNeeds(next);
    setBranch((prev) => Object.fromEntries(Object.entries(prev).filter(([dep]) => next.includes(dep))));
    setDirty(true);
  }

  function toggleLabel(dep: string, label: string) {
    setBranch((prev) => {
      const now = prev[dep] ?? [];
      const nextLabels = now.includes(label) ? now.filter((l) => l !== label) : [...now, label];
      const next = { ...prev };
      if (nextLabels.length) next[dep] = nextLabels;
      else delete next[dep];
      return next;
    });
    setDirty(true);
  }

  function apply() {
    if (parseError) return;
    onApplyConfig(current.id, config, { title: title.trim() || current.id, needs, branch });
    setDirty(false);
  }

  const gateable = needs.map((dep) => ({ dep, labels: declaredLabels(nodes.find((n) => n.id === dep)) })).filter((g) => g.labels.length > 0);

  return (
    <aside className="fs-inspector" data-testid="node-inspector">
      <header className="fs-inspector__head">
        <h3>{current.title || current.id}</h3>
        <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} testId="node-inspector-close" />
      </header>

      <dl className="fs-inspector__facts">
        <div>
          <dt>{t('Id')}</dt>
          <dd><code>{current.id}</code></dd>
        </div>
        <div>
          <dt>{t('Type')}</dt>
          <dd><code>{current.type}</code></dd>
        </div>
        {(skill || backend) && (
          <div>
            <dt>{t('Tool')}</dt>
            <dd>{skill || backend}</dd>
          </div>
        )}
        {model && (
          <div>
            <dt>{t('Model')}</dt>
            <dd>{model}</dd>
          </div>
        )}
      </dl>

      <div className="fs-form">
        <div className="fs-form__field">
          <label className="fs-form__label" htmlFor="node-inspector-title">{t('Title')}</label>
          <input id="node-inspector-title" className="fs-form__control" type="text" value={title} data-testid="node-inspector-title"
            onChange={(e) => { setTitle(e.target.value); setDirty(true); }} />
        </div>

        <fieldset className="fs-form__group" data-testid="node-inspector-needs">
          <legend>{t('Needs (entry contract)')}</legend>
          {others.length === 0 && <p className="fs-form__hint">{t('none — a root node')}</p>}
          {others.map((n) => (
            <label key={n.id} className="fs-form__inline">
              <input type="checkbox" checked={needs.includes(n.id)} onChange={() => toggleNeed(n.id)} data-testid={`node-inspector-need-${n.id}`} />
              <code>{n.id}</code> <span className="fs-form__hint">{n.type}</span>
            </label>
          ))}
        </fieldset>

        {gateable.length > 0 && (
          <fieldset className="fs-form__group" data-testid="node-inspector-branch">
            <legend>{t('Run only on this branch')}</legend>
            <p className="fs-form__hint">{t('Leave all unchecked to run whichever way the node above went.')}</p>
            {gateable.map(({ dep, labels }) => (
              <div key={dep} className="fs-form__kinds" role="group" aria-label={dep}>
                <code>{dep}</code>
                {labels.map((label) => (
                  <label key={label} className="fs-form__inline">
                    <input type="checkbox" checked={(branch[dep] ?? []).includes(label)} onChange={() => toggleLabel(dep, label)}
                      data-testid={`node-inspector-branch-${dep}-${label}`} />
                    {label}
                  </label>
                ))}
              </div>
            ))}
          </fieldset>
        )}

        {hasForm && <ConfigForm type={current.type} config={config} onChange={editConfig} ctx={{ nodes: nodes.length ? nodes : [self], self, profiles }} />}
      </div>

      <details className="fs-inspector__raw" open={!hasForm}>
        <summary>{t('config (JSON) — changing the tool or model re-runs preflight and lint')}</summary>
        <label className="fs-inspector__config-label" htmlFor="node-inspector-config">{t('config (JSON)')}</label>
        <textarea
          id="node-inspector-config"
          className="fs-inspector__config"
          value={raw}
          onChange={(e) => editRaw(e.target.value)}
          spellCheck={false}
          rows={10}
          data-testid="node-inspector-config"
        />
      </details>
      {parseError && (
        <p className="fs-inspector__error" role="alert">{parseError}</p>
      )}
      <div className="fs-inspector__actions">
        <Button variant="primary" size="sm" label={t('Apply and re-check')} onClick={apply} loading={busy} disabled={!dirty || !!parseError} testId="node-inspector-apply" />
        {onRemove && (
          <Button variant="danger" size="sm" label={t('Remove node')} onClick={() => onRemove(current.id)} testId="node-inspector-remove" />
        )}
      </div>

      {relevant.length > 0 && (
        <div className="fs-inspector__lint" data-testid="node-inspector-lint">
          <h4>{t('Lint findings for this node')}</h4>
          <ul>
            {relevant.map((w, i) => (
              <li key={`${w.code}-${i}`} data-severity={w.severity}>
                <code>{w.code}</code>
                <p>{w.message}</p>
                {w.hint && <p className="fs-inspector__hint">{w.hint}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </aside>
  );
}
