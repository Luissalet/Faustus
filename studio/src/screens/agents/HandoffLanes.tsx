import { AlertTriangle, CheckCircle2, Play, Plus, RefreshCw, Trash2, XCircle } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { Button, Dialog, MermaidView, Skeleton } from '../../components';
import {
  getHandoffLanes,
  getHandoffLanesGraph,
  saveHandoffLanes,
  testHandoffLane,
  type HandoffDecision,
  type HandoffLane,
  type HandoffLanesMode,
} from '../../adapters/handoffLanes';
import { t } from '../../i18n';

/**
 * Lot E — permissions-as-topology: which agent may delegate to which, with
 * which tools (`docs/api/handoff_lanes.md`, `src/handoff_lanes.py`). Sibling
 * of `ProfileLint.tsx`'s dialog, opened from `Defs.tsx`'s toolbar.
 */

const MODES: HandoffLanesMode[] = ['off', 'shadow', 'enforce'];
const MODE_LABEL: Record<HandoffLanesMode, string> = {
  off: 'Off — unchanged behaviour',
  shadow: 'Shadow — evaluate and log, never block',
  enforce: 'Enforce — refuse uncovered handoffs',
};

function csv(list?: string[]): string {
  return (list ?? []).join(', ');
}

function fromCsv(value: string): string[] | undefined {
  const items = value.split(',').map((s) => s.trim()).filter(Boolean);
  return items.length ? items : undefined;
}

function emptyLane(n: number): HandoffLane {
  return { id: `lane${n}`, from: 'main', to: '*' };
}

export function HandoffLanesPanel({ onClose }: { onClose: () => void }) {
  const [lanes, setLanes] = useState<HandoffLane[] | null>(null);
  const [mode, setMode] = useState<HandoffLanesMode>('off');
  const [mermaid, setMermaid] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [testFrom, setTestFrom] = useState('main');
  const [testTo, setTestTo] = useState('coder');
  const [testTools, setTestTools] = useState('bash, write_file');
  const [testResult, setTestResult] = useState<HandoffDecision | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const [state, graph] = await Promise.all([getHandoffLanes(), getHandoffLanesGraph()]);
      setLanes(state.lanes);
      setMode(state.mode);
      setMermaid(graph.mermaid);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (next: HandoffLane[], nextMode: HandoffLanesMode) => {
    setSaving(true);
    setSaveError(null);
    try {
      const state = await saveHandoffLanes(next, nextMode);
      setLanes(state.lanes);
      setMode(state.mode);
      const graph = await getHandoffLanesGraph();
      setMermaid(graph.mermaid);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const updateLane = (i: number, patch: Partial<HandoffLane>) => {
    if (!lanes) return;
    const next = lanes.slice();
    next[i] = { ...next[i], ...patch };
    setLanes(next);
  };

  const addLane = () => {
    const next = [...(lanes ?? []), emptyLane((lanes?.length ?? 0) + 1)];
    setLanes(next);
  };

  const removeLane = (i: number) => {
    if (!lanes) return;
    const next = lanes.slice();
    next.splice(i, 1);
    setLanes(next);
  };

  const runTest = async () => {
    setTestError(null);
    setTestResult(null);
    try {
      const tools = testTools.split(',').map((s) => s.trim()).filter(Boolean);
      setTestResult(await testHandoffLane(testFrom.trim() || 'main', testTo.trim() || '*', tools));
    } catch (e) {
      setTestError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Dialog open onOpenChange={(next) => { if (!next) onClose(); }} title={t('Handoff lanes')} testId="handoff-lanes">
      <div className="fs-lint">
        <p className="fs-prose">
          {t('Which agent may delegate to which, with which tools — an explicit, inspectable policy. Off by default: nothing changes until a mode below is picked.')}
        </p>

        <div className="fs-lint__toolbar">
          <label className="fs-agents__search">
            {t('Mode')}
            <select value={mode} onChange={(e) => setMode(e.target.value as HandoffLanesMode)} data-testid="handoff-lanes-mode" style={{ marginLeft: 8 }}>
              {MODES.map((m) => (
                <option key={m} value={m}>
                  {t(MODE_LABEL[m])}
                </option>
              ))}
            </select>
          </label>
          <span className="fs-lint__spacer" />
          <Button variant="ghost" size="sm" icon={RefreshCw} label={t('Refresh')} loading={busy} onClick={() => void load()} testId="handoff-lanes-refresh" />
          <Button
            variant="primary"
            size="sm"
            icon={CheckCircle2}
            label={saving ? t('Saving…') : t('Save')}
            loading={saving}
            onClick={() => void save(lanes ?? [], mode)}
            testId="handoff-lanes-save"
          />
        </div>

        {error && <div className="fs-wk__error">{t('Could not read the handoff lanes')}: {error}</div>}
        {saveError && <div className="fs-wk__error">{t('Could not save')}: {saveError}</div>}
        {lanes === null && !error && <Skeleton label={t('Loading handoff lanes')} height="40px" count={3} />}

        {lanes !== null && (
          <table className="fs-def__table" data-testid="handoff-lanes-table">
            <thead>
              <tr>
                <th>{t('Id')}</th>
                <th>{t('From')}</th>
                <th>{t('To')}</th>
                <th>{t('Tools allow')}</th>
                <th>{t('Tools deny')}</th>
                <th>{t('Max depth')}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {lanes.map((lane, i) => (
                <tr key={i} data-testid="handoff-lane-row">
                  <td><input value={lane.id} onChange={(e) => updateLane(i, { id: e.target.value })} /></td>
                  <td><input value={lane.from} onChange={(e) => updateLane(i, { from: e.target.value })} /></td>
                  <td><input value={lane.to} onChange={(e) => updateLane(i, { to: e.target.value })} /></td>
                  <td><input value={csv(lane.tools_allow)} onChange={(e) => updateLane(i, { tools_allow: fromCsv(e.target.value) })} /></td>
                  <td><input value={csv(lane.tools_deny)} onChange={(e) => updateLane(i, { tools_deny: fromCsv(e.target.value) })} /></td>
                  <td>
                    <input
                      type="number"
                      min={1}
                      value={lane.max_depth ?? ''}
                      onChange={(e) => updateLane(i, { max_depth: e.target.value ? Number(e.target.value) : undefined })}
                      style={{ width: 60 }}
                    />
                  </td>
                  <td>
                    <IconOnlyRemove onClick={() => removeLane(i)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {lanes !== null && (
          <Button variant="secondary" size="sm" icon={Plus} label={t('Add lane')} onClick={addLane} testId="handoff-lanes-add" />
        )}

        <div className="fs-lint__toolbar" style={{ marginTop: 16 }}>
          <strong>{t('Test a handoff')}</strong>
        </div>
        <div className="fs-agents__toolbar">
          <input placeholder={t('from (e.g. main)')} value={testFrom} onChange={(e) => setTestFrom(e.target.value)} data-testid="handoff-test-from" />
          <input placeholder={t('to (e.g. coder)')} value={testTo} onChange={(e) => setTestTo(e.target.value)} data-testid="handoff-test-to" />
          <input placeholder={t('tools (comma separated)')} value={testTools} onChange={(e) => setTestTools(e.target.value)} style={{ flex: 1 }} />
          <Button variant="secondary" size="sm" icon={Play} label={t('Test')} onClick={() => void runTest()} testId="handoff-test-run" />
        </div>
        {testError && <div className="fs-wk__error">{testError}</div>}
        {testResult && (
          <p className="fs-lint__summary" data-testid="handoff-test-result">
            {testResult.allowed ? <CheckCircle2 size={14} aria-hidden="true" /> : <XCircle size={14} aria-hidden="true" />}{' '}
            {testResult.allowed ? t('Allowed') : t('Refused')}
            {testResult.lane_id ? ` — ${t('lane')} ${testResult.lane_id}` : ''}
            {testResult.disabled_tools.length > 0 ? ` — ${t('tools taken back')}: ${testResult.disabled_tools.join(', ')}` : ''}
            <br />
            <span className="fs-lint__hint">{testResult.reason}</span>
          </p>
        )}

        <div className="fs-lint__toolbar" style={{ marginTop: 16 }}>
          <strong>{t('Diagram')}</strong>
        </div>
        {mermaid && <MermaidView code={mermaid} filename="handoff-lanes.mmd" />}
      </div>
    </Dialog>
  );
}

function IconOnlyRemove({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="ghost" size="sm" icon={Trash2} label="" title={t('Remove lane')} onClick={onClick} testId="handoff-lane-remove" />
  );
}
