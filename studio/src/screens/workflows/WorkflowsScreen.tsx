import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { Compass, Download, FileJson, Play, Workflow as WorkflowIcon } from 'lucide-react';
import { Button, EmptyState, Skeleton } from '../../components';
import { t } from '../../i18n';
import { getWorkflowRunDefinition, loadActivity, type ActivityRun } from '../../adapters/activity';
import {
  exportWorkflowDefinition,
  importWorkflowDefinition,
  workflowPreflight,
  workflowSimulate,
  type Preflight,
  type SimulationResult,
} from '../../adapters/topology';
import { PlanGraph, type NodeMark, type PlanGraphNode } from './PlanGraph';
import { NodeInspector, type InspectorNode } from './NodeInspector';
import { RunOverlay } from './RunOverlay';
import './workflows.css';

/**
 * W2-E (CMP-07, INFORME §3.6) — "diseñar, simular y depurar un plan".
 *
 * `/workflows`: one screen, three explicit modes over the SAME definition —
 * Design (the plan as drawn, `NodeInspector` on each node, live lint),
 * Structural simulation (`workflowSimulate`, choices per `condition`/
 * `human_approval`, never guessed past), and Execution (`RunOverlay`, real
 * and confirmed). Chat and this canvas edit the same `definition` object —
 * there is no second, canvas-only format; a hand-adjusted layout is
 * exported separately via `interchange.export_canonical`'s `layout` field
 * (`adapters/topology.ts::exportWorkflowDefinition`), never merged into
 * what actually runs.
 */

type Mode = 'design' | 'simulate' | 'execute';

interface RawNode {
  id: string;
  type: string;
  title?: string;
  needs?: string[];
  config?: Record<string, unknown>;
}

function rawNodesOf(definition: Record<string, unknown> | null): RawNode[] {
  return Array.isArray(definition?.nodes) ? (definition!.nodes as RawNode[]) : [];
}

function planNodesOf(definition: Record<string, unknown> | null): PlanGraphNode[] {
  return rawNodesOf(definition).map((n) => ({
    id: String(n.id), type: String(n.type), title: String(n.title || n.id),
    needs: Array.isArray(n.needs) ? n.needs.map(String) : [],
  }));
}

function findInspectorNode(definition: Record<string, unknown> | null, nodeId: string | null): InspectorNode | null {
  if (!nodeId) return null;
  const node = rawNodesOf(definition).find((n) => n.id === nodeId);
  if (!node) return null;
  return { id: node.id, type: node.type, title: node.title || node.id, needs: node.needs ?? [], config: node.config ?? {} };
}

// ---------------------------------------------------------------------------
// W3-F (CONTRATO_W3.md) — a hand-adjusted node layout (PlanGraph's new
// `onNodeMove`) is worth remembering per DEFINITION, not per session: the
// same plan reopened later — from Recent runs or a paste — should come back
// the way it was left. There is no server-side identity to key on until a
// run exists (a pasted/imported definition has none), so the key is a
// content fingerprint of the definition itself: a plain, non-cryptographic
// hash (FNV-1a over a stably-ordered JSON.stringify) — good enough to tell
// "the same plan" from "a different one", not a security boundary. A
// coincidental collision only means an unrelated plan's old layout gets
// reused as a starting point, never a wrong RUN or a wrong export target.
// ---------------------------------------------------------------------------

function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`;
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => JSON.stringify(k) + ':' + stableStringify(v)).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

function definitionFingerprint(definition: Record<string, unknown> | null): string {
  if (!definition) return '';
  const s = stableStringify(definition);
  let hash = 0;
  for (let i = 0; i < s.length; i++) hash = (Math.imul(31, hash) + s.charCodeAt(i)) | 0;
  return (hash >>> 0).toString(36);
}

const LAYOUT_KEY_PREFIX = 'faustus_studio_workflows_layout:';

function loadLayout(fingerprint: string): Record<string, { x: number; y: number }> {
  if (!fingerprint) return {};
  try {
    const raw = localStorage.getItem(LAYOUT_KEY_PREFIX + fingerprint);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    return parsed as Record<string, { x: number; y: number }>;
  } catch {
    return {};
  }
}

function saveLayout(fingerprint: string, layout: Record<string, { x: number; y: number }>): void {
  if (!fingerprint) return;
  try {
    localStorage.setItem(LAYOUT_KEY_PREFIX + fingerprint, JSON.stringify(layout));
  } catch {
    // best-effort only (private browsing / quota) — a lost layout is never
    // data loss, just a redraw back to the computed default next time.
  }
}

function downloadJson(data: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  window.setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

export function WorkflowsScreen() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [mode, setMode] = useState<Mode>('design');
  const [definition, setDefinition] = useState<Record<string, unknown> | null>(null);
  const [boundRunId, setBoundRunId] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  const [recentRuns, setRecentRuns] = useState<ActivityRun[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);

  const [paste, setPaste] = useState('');
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadNotice, setLoadNotice] = useState<string | null>(null);

  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [preflightBusy, setPreflightBusy] = useState(false);
  const [preflightError, setPreflightError] = useState<string | null>(null);

  const [simulation, setSimulation] = useState<SimulationResult | null>(null);
  const [simChoices, setSimChoices] = useState<Record<string, boolean | undefined>>({});
  const [roundsMax, setRoundsMax] = useState(25);
  const [simBusy, setSimBusy] = useState(false);
  const [simError, setSimError] = useState<string | null>(null);

  // W3-F: node layout, persisted in localStorage per definition fingerprint
  // (see `loadLayout`/`saveLayout` above), and the Export button's state.
  const [layout, setLayout] = useState<Record<string, { x: number; y: number }>>({});
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const fingerprint = useMemo(() => definitionFingerprint(definition), [definition]);
  const openedRunParam = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadActivity()
      .then((feed) => {
        if (cancelled) return;
        const workflows = feed.runs
          .filter((r): r is ActivityRun => r.kind === 'workflow')
          .sort((a, b) => (b.startedAt || '').localeCompare(a.startedAt || ''));
        setRecentRuns(workflows);
      })
      .catch((e) => {
        if (!cancelled) setRunsError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // W3-F: a definition's hand-adjusted layout is loaded once per fingerprint
  // (a new definition — pasted, imported, or loaded from a different run —
  // starts from whatever this browser saved for THAT content before, or the
  // computed default when there is none yet).
  useEffect(() => {
    setLayout(loadLayout(fingerprint));
  }, [fingerprint]);

  const onNodeMove = useCallback(
    (id: string, pos: { x: number; y: number }) => {
      setLayout((prev) => {
        const next = { ...prev, [id]: pos };
        saveLayout(fingerprint, next);
        return next;
      });
    },
    [fingerprint],
  );

  const nodes = useMemo(() => planNodesOf(definition), [definition]);
  const rawNodes = useMemo(() => rawNodesOf(definition), [definition]);
  const gateNodeIds = useMemo(
    () => rawNodes.filter((n) => n.type === 'condition' || n.type === 'human_approval').map((n) => n.id),
    [rawNodes],
  );

  const runPreflight = useCallback((def: Record<string, unknown>) => {
    setPreflightBusy(true);
    setPreflightError(null);
    workflowPreflight(def)
      .then(setPreflight)
      .catch((e) => setPreflightError(e instanceof Error ? e.message : String(e)))
      .finally(() => setPreflightBusy(false));
  }, []);

  useEffect(() => {
    if (definition) runPreflight(definition);
    else {
      setPreflight(null);
      setPreflightError(null);
    }
    setSimulation(null);
    setSimError(null);
    setSimChoices({});
  }, [definition, runPreflight]);

  // W3-F: shared by the "Recent runs" list AND the `?run=<id>` deep link —
  // one code path loads a run's definition into `RunOverlay`'s Execute mode
  // either way, so a shared/bookmarked `/workflows?run=<id>` behaves exactly
  // like clicking that run in the list.
  const openRun = useCallback((runId: string) => {
    setLoadError(null);
    setLoadNotice(null);
    getWorkflowRunDefinition(runId)
      .then((def) => {
        setDefinition(def);
        setBoundRunId(runId);
        setSelectedNodeId(null);
        setMode('execute');
        // Mark this id as already-opened BEFORE the param write below so
        // the deep-link effect (watching `searchParams`) does not treat its
        // own resulting change as a second, unopened `?run=` to load.
        openedRunParam.current = runId;
        setSearchParams((prev) => {
          const next = new URLSearchParams(prev);
          next.set('run', runId);
          return next;
        }, { replace: true });
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)));
  }, [setSearchParams]);

  const loadFromRun = useCallback((run: ActivityRun) => openRun(run.id), [openRun]);

  // A brand-new run started from Design/Execute (RunOverlay's own confirmed
  // "Start for real") gets the same URL treatment as one loaded from
  // history, so refreshing mid-run does not lose it either.
  const onRunStarted = useCallback((runId: string) => {
    setBoundRunId(runId);
    openedRunParam.current = runId;
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('run', runId);
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  // Deep link: `/workflows?run=<id>` loads that run on arrival. Guarded by
  // `openedRunParam` so it fires once per distinct `run` value, not on every
  // render (`openRun` itself rewrites the same param via `setSearchParams`).
  useEffect(() => {
    const runId = searchParams.get('run');
    if (!runId || openedRunParam.current === runId) return;
    openedRunParam.current = runId;
    openRun(runId);
  }, [searchParams, openRun]);

  // A paste/import replaces the run this screen was bound to (if any) — drop
  // the `?run=` deep link along with it, so the address bar does not keep
  // pointing at a run that is no longer what is on screen.
  const clearRunParam = useCallback(() => {
    openedRunParam.current = null;
    setSearchParams((prev) => {
      if (!prev.has('run')) return prev;
      const next = new URLSearchParams(prev);
      next.delete('run');
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  const loadFromPaste = useCallback(() => {
    setLoadError(null);
    setLoadNotice(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(paste);
    } catch {
      setLoadError(t('That is not valid JSON.'));
      return;
    }
    if (parsed && typeof parsed === 'object' && Array.isArray((parsed as Record<string, unknown>).nodes)) {
      setDefinition(parsed as Record<string, unknown>);
      setBoundRunId(null);
      setSelectedNodeId(null);
      setMode('design');
      clearRunParam();
      return;
    }
    importWorkflowDefinition(parsed)
      .then((result) => {
        if (result.definition) {
          setDefinition(result.definition);
          setBoundRunId(null);
          setSelectedNodeId(null);
          setMode('design');
          clearRunParam();
          if (result.designOnly.length > 0) {
            setLoadNotice(t('{n} node(s) were kept as design-only — not translated into anything executable.', { n: result.designOnly.length }));
          }
        } else {
          setLoadError(t('Nothing executable came out of that import — see the rejected entries.'));
        }
      })
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)));
  }, [paste, clearRunParam]);

  const applyNodeConfig = useCallback(
    (nodeId: string, config: Record<string, unknown>) => {
      if (!definition) return;
      setDefinition({ ...definition, nodes: rawNodes.map((n) => (n.id === nodeId ? { ...n, config } : n)) });
    },
    [definition, rawNodes],
  );

  // W3-F: the canonical envelope from `exportWorkflowDefinition` (round-trips
  // through `/import` unchanged — `docs/api/topology.md` §Interchange),
  // carrying this browser's saved `layout` so a hand-adjusted plan survives
  // the download too. A file, not a copy-paste — the JSON textarea above is
  // for the reverse direction (loading one).
  const exportDefinition = useCallback(() => {
    if (!definition) return;
    setExportBusy(true);
    setExportError(null);
    exportWorkflowDefinition(definition, { layout })
      .then((envelope) => {
        const workflowId = typeof definition.id === 'string' && definition.id ? definition.id : 'workflow';
        downloadJson(envelope, `${workflowId}.json`);
      })
      .catch((e) => setExportError(e instanceof Error ? e.message : String(e)))
      .finally(() => setExportBusy(false));
  }, [definition, layout]);

  const runSimulation = useCallback(() => {
    if (!definition) return;
    setSimBusy(true);
    setSimError(null);
    const choices: Record<string, boolean> = {};
    for (const [id, value] of Object.entries(simChoices)) if (value !== undefined) choices[id] = value;
    workflowSimulate(definition, { choices, roundsMax })
      .then(setSimulation)
      .catch((e) => setSimError(e instanceof Error ? e.message : String(e)))
      .finally(() => setSimBusy(false));
  }, [definition, simChoices, roundsMax]);

  const simMarks = useMemo<Record<string, NodeMark>>(() => {
    if (!simulation) return {};
    const marks: Record<string, NodeMark> = {};
    for (const id of simulation.activated) marks[id] = 'activated';
    for (const id of simulation.notTaken) marks[id] = 'not_taken';
    for (const id of Object.keys(simulation.awaitingChoice)) marks[id] = 'awaiting_choice';
    for (const id of simulation.humanWaits) if (!marks[id]) marks[id] = 'human_wait';
    return marks;
  }, [simulation]);

  function openNodeFromFindingSubject(subject: string) {
    const match = /^node:(.+)$/.exec(subject);
    if (match) setSelectedNodeId(match[1]);
  }

  const selectedInspectorNode = findInspectorNode(definition, selectedNodeId);

  return (
    <div className="fs-screen fs-workflows" data-testid="workflows-screen">
      <header className="fs-screen__head">
        <div className="fs-workflows__title">
          <h1 className="fs-screen__title">
            <WorkflowIcon size={20} aria-hidden="true" /> {t('Workflows')}
          </h1>
          <p className="fs-screen__sub">
            {t('Design a plan, simulate it structurally, then authorize a real run — the same definition throughout.')}
          </p>
        </div>
        <div className="fs-workflows__modes" role="tablist" aria-label={t('Mode')}>
          {(['design', 'simulate', 'execute'] as const).map((m) => (
            <button
              key={m}
              type="button"
              role="tab"
              aria-selected={mode === m}
              className="fs-chip"
              data-on={mode === m || undefined}
              onClick={() => setMode(m)}
              data-testid={`workflows-mode-${m}`}
            >
              {m === 'design' ? t('Design') : m === 'simulate' ? t('Structural simulation') : t('Authorized real execution')}
            </button>
          ))}
        </div>
        <Button
          variant="secondary"
          size="sm"
          icon={Download}
          label={t('Export')}
          title={t('Download this definition as JSON, including your saved node layout.')}
          onClick={exportDefinition}
          loading={exportBusy}
          disabled={!definition}
          testId="workflows-export"
        />
      </header>
      {exportError && <p className="fs-workflows__error" role="alert">{exportError}</p>}

      <div className="fs-workflows__source">
        <div className="fs-workflows__recent">
          <h2>{t('Recent runs')}</h2>
          {recentRuns === null && !runsError && <Skeleton label={t('Loading recent workflow runs')} count={2} height="40px" />}
          {runsError && <p className="fs-workflows__error" role="alert">{runsError}</p>}
          {recentRuns && recentRuns.length === 0 && <p className="fs-workflows__empty">{t('No workflow runs yet.')}</p>}
          {recentRuns && recentRuns.length > 0 && (
            <ul className="fs-workflows__runlist">
              {recentRuns.slice(0, 8).map((r) => (
                <li key={r.id}>
                  <button type="button" className="fs-workflows__runbtn" onClick={() => loadFromRun(r)} data-testid={`workflows-load-run-${r.id}`}>
                    <span>{r.title}</span>
                    <span data-status={r.status}>{r.status}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="fs-workflows__paste">
          <h2>
            <FileJson size={14} aria-hidden="true" /> {t('Import or paste JSON')}
          </h2>
          <textarea
            className="fs-workflows__pastearea"
            value={paste}
            onChange={(e) => setPaste(e.target.value)}
            placeholder={t('A workflow definition, this module’s canonical export, or an aigraphstudio-shaped graph…')}
            rows={4}
            spellCheck={false}
            data-testid="workflows-paste"
          />
          <Button variant="secondary" size="sm" label={t('Load')} onClick={loadFromPaste} testId="workflows-load-paste" />
          {loadError && <p className="fs-workflows__error" role="alert">{loadError}</p>}
          {loadNotice && <p className="fs-workflows__notice">{loadNotice}</p>}
        </div>
      </div>

      {!definition && (
        <EmptyState
          icon={Compass}
          title={t('Nothing loaded yet')}
          body={t('Pick a recent run, or paste a definition, to see its plan.')}
        />
      )}

      {definition && (
        <div className="fs-workflows__body">
          <div className="fs-workflows__canvas">
            {mode === 'design' && (
              <>
                <PlanGraph nodes={nodes} selectedNodeId={selectedNodeId} onSelectNode={setSelectedNodeId} layout={layout} onNodeMove={onNodeMove} />
                <div className="fs-workflows__preflight">
                  {preflightBusy && <Skeleton label={t('Running preflight')} count={2} height="28px" />}
                  {preflightError && <p className="fs-workflows__error" role="alert">{preflightError}</p>}
                  {preflight && (
                    <>
                      <p className="fs-workflows__preflight-summary">
                        {t('Connections')}: {preflight.connections.join(', ') || '—'} · {t('Human waits')}: {preflight.humanWaits.length}
                      </p>
                      {preflight.warnings.length > 0 && (
                        <ul className="fs-workflows__lint" data-testid="workflows-lint-list">
                          {preflight.warnings.map((w, i) => (
                            <li key={`${w.code}-${i}`} data-severity={w.severity}>
                              <button
                                type="button"
                                className="fs-workflows__lint-open"
                                onClick={() => openNodeFromFindingSubject(w.subject)}
                                data-testid={`workflows-lint-open-${i}`}
                              >
                                <code>{w.code}</code> — {w.message}
                              </button>
                              {w.hint && <p className="fs-workflows__hint">{w.hint}</p>}
                            </li>
                          ))}
                        </ul>
                      )}
                    </>
                  )}
                </div>
              </>
            )}

            {mode === 'simulate' && (
              <>
                <div className="fs-workflows__simcontrols">
                  {gateNodeIds.length === 0 && (
                    <p className="fs-workflows__empty">{t('No condition or human_approval nodes to choose for.')}</p>
                  )}
                  {gateNodeIds.map((id) => (
                    <div key={id} className="fs-workflows__choice" role="group" aria-label={id}>
                      <span>{id}</span>
                      {(['pass', 'unset', 'fail'] as const).map((v) => (
                        <button
                          key={v}
                          type="button"
                          className="fs-chip"
                          data-on={
                            (v === 'pass' && simChoices[id] === true) ||
                            (v === 'fail' && simChoices[id] === false) ||
                            (v === 'unset' && simChoices[id] === undefined) ||
                            undefined
                          }
                          onClick={() => setSimChoices((prev) => ({ ...prev, [id]: v === 'pass' ? true : v === 'fail' ? false : undefined }))}
                          data-testid={`workflows-choice-${id}-${v}`}
                        >
                          {v === 'pass' ? t('Assume passes/approved') : v === 'fail' ? t('Assume fails/denied') : t('Undecided')}
                        </button>
                      ))}
                    </div>
                  ))}
                  <label className="fs-workflows__rounds">
                    {t('Rounds max')}
                    <input
                      type="number"
                      min={1}
                      max={200}
                      value={roundsMax}
                      onChange={(e) => setRoundsMax(Math.max(1, Math.min(200, Number(e.target.value) || 25)))}
                      data-testid="workflows-rounds-max"
                    />
                  </label>
                  <Button variant="primary" size="sm" icon={Play} label={t('Run simulation')} onClick={runSimulation} loading={simBusy} testId="workflows-run-simulation" />
                </div>
                {simError && <p className="fs-workflows__error" role="alert">{simError}</p>}
                <PlanGraph nodes={nodes} marks={simMarks} selectedNodeId={selectedNodeId} onSelectNode={setSelectedNodeId} layout={layout} />
                {simulation && (
                  <div className="fs-workflows__simresult" data-testid="workflows-simulation-result">
                    <p>
                      {t('{a} activated · {n} not taken · {h} human wait(s) · {w} awaiting a choice', {
                        a: simulation.activated.length,
                        n: simulation.notTaken.length,
                        h: simulation.humanWaits.length,
                        w: Object.keys(simulation.awaitingChoice).length,
                      })}
                    </p>
                    <ul className="fs-workflows__warnings">
                      {simulation.warnings.map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}

            {mode === 'execute' && (
              <RunOverlay
                definition={definition}
                runId={boundRunId}
                onRunStarted={onRunStarted}
                selectedNodeId={selectedNodeId}
                onSelectNode={setSelectedNodeId}
              />
            )}
          </div>

          <NodeInspector
            node={selectedInspectorNode}
            warnings={preflight?.warnings ?? []}
            onClose={() => setSelectedNodeId(null)}
            onApplyConfig={applyNodeConfig}
            busy={preflightBusy}
          />
        </div>
      )}
    </div>
  );
}
