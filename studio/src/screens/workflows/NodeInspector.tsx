import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { t } from '../../i18n';
import type { LintFinding } from '../../adapters/topology';

/**
 * W2-E (CMP-07): the contract of one node — entry/exit (`needs`), the
 * tool/model it names, and its raw `config` — editable as JSON. Changing
 * `config` here never runs anything by itself: `WorkflowsScreen` re-runs
 * `workflowPreflight` (which re-runs `agent_profile_lint.lint_workflow`)
 * against the edited definition, and this panel shows whatever findings
 * now name this node.
 */

export interface InspectorNode {
  id: string;
  type: string;
  title: string;
  needs: string[];
  config: Record<string, unknown>;
}

export interface NodeInspectorProps {
  node: InspectorNode | null;
  warnings: LintFinding[];
  onClose: () => void;
  onApplyConfig: (nodeId: string, config: Record<string, unknown>) => void;
  busy?: boolean;
}

/** `agent_profile_lint`'s `subject` for a workflow finding is `"node:<id>"`
 *  (most rules) or `"workflow:<id1>->...->id"` (a cycle) — match both. */
function subjectMatchesNode(subject: string, nodeId: string): boolean {
  return subject === `node:${nodeId}` || subject.split('->').includes(nodeId);
}

export function NodeInspector({ node, warnings, onClose, onApplyConfig, busy }: NodeInspectorProps) {
  const [draft, setDraft] = useState('');
  const [parseError, setParseError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(node ? JSON.stringify(node.config, null, 2) : '');
    setParseError(null);
  }, [node?.id]);

  if (!node) {
    return (
      <aside className="fs-inspector fs-inspector--empty" data-testid="node-inspector-empty">
        <p>{t('Select a node in the plan to inspect its contract.')}</p>
      </aside>
    );
  }

  const current = node; // narrowed non-null once, so the closure below keeps that type
  const relevant = warnings.filter((w) => subjectMatchesNode(w.subject, current.id));
  const model = typeof current.config.model === 'string' ? current.config.model : '';
  const skill = typeof current.config.skill === 'string' ? current.config.skill : '';
  const backend = typeof current.config.backend === 'string' ? current.config.backend : '';

  function apply() {
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft || '{}');
    } catch {
      setParseError(t('config must be valid JSON.'));
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setParseError(t('config must be a JSON object.'));
      return;
    }
    setParseError(null);
    onApplyConfig(current.id, parsed as Record<string, unknown>);
  }

  return (
    <aside className="fs-inspector" data-testid="node-inspector">
      <header className="fs-inspector__head">
        <h3>{node.title || node.id}</h3>
        <Button variant="ghost" size="sm" label={t('Close')} onClick={onClose} testId="node-inspector-close" />
      </header>

      <dl className="fs-inspector__facts">
        <div>
          <dt>{t('Id')}</dt>
          <dd><code>{node.id}</code></dd>
        </div>
        <div>
          <dt>{t('Type')}</dt>
          <dd><code>{node.type}</code></dd>
        </div>
        <div>
          <dt>{t('Needs (entry contract)')}</dt>
          <dd>{node.needs.length ? node.needs.join(', ') : t('none — a root node')}</dd>
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

      <label className="fs-inspector__config-label" htmlFor="node-inspector-config">
        {t('config (JSON) — changing the tool or model re-runs preflight and lint')}
      </label>
      <textarea
        id="node-inspector-config"
        className="fs-inspector__config"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
        rows={10}
        data-testid="node-inspector-config"
      />
      {parseError && (
        <p className="fs-inspector__error" role="alert">{parseError}</p>
      )}
      <Button variant="primary" size="sm" label={t('Apply and re-check')} onClick={apply} loading={busy} testId="node-inspector-apply" />

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
