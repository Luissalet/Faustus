/**
 * Agent loop of a turn or a sub-agent: plan -> act -> observe.
 *
 * Read from what the chat already received, never from elapsed time: model
 * text or reasoning is plan, a running tool is act, a tool result with no new
 * round started yet is observe. The practice checklist is marked from the
 * commands the tools actually ran (same rules as src/agent_loop_state.py).
 */
import type { Step, Turn, Worker } from '../../screens/studio/model';

export type LoopPhase = 'plan' | 'act' | 'observe' | 'done' | 'idle';
export type PracticeState = 'ok' | 'fail' | 'unknown' | 'pending';

export interface LoopStep {
  kind: 'plan' | 'tool' | 'result' | 'commit' | 'submit' | 'end';
  label: string;
  lap: number;
  ok?: boolean;
}

export interface Practice {
  id: string;
  label: string;
  state: PracticeState;
  evidence: string;
}

export interface LoopView {
  phase: LoopPhase;
  lap: number;
  tool: string;
  elapsedS: number | null;
  tokens: number | null;
  steps: LoopStep[];
  practices: Practice[];
}

export const PRACTICES: [string, string][] = [
  ['tests', 'Tests run'],
  ['browser', 'App checked in a browser'],
  ['docs', 'Docs updated (FAUSTUS.md / README)'],
  ['commit', 'Commit with the right identity'],
  ['no_evidence', 'No evidence files committed'],
  ['agora', 'Delivered in the Agora (submit / done)'],
];

const EXPECTED_AUTHOR = 'luissalet';
const TESTS = /\b(pytest|py\.test|unittest|vitest|jest|tox|(?:npm|pnpm|yarn)(?: run)? (?:test|check)|go test|cargo test|node --test)\b/i;
const BROWSER = /playwright|puppeteer|chromium|headless|browser/i;
const DOCS = /\b(FAUSTUS|README(?:\.es)?|AGENTS)\.md\b/i;
const WRITE_TOOL = /write|edit|patch|replace|create_file|str_replace/i;
const COMMIT = /\bgit\b[^\n|;&]*\bcommit\b/i;
const ADD = /\bgit\b[^\n|;&]*\badd\b/i;
const AUTHOR = /--author[= ]+["']?([^<"']+)|user\.name=["']?([^"'\s]+)|GIT_AUTHOR_NAME=["']?([^"'\s]+)/;
const TRAILER = /Co-Authored-By:/i;
const EVIDENCE = /\.(png|jpe?g|webp|gif|mp4|webm|har)\b|evidenc|screenshot|captura|qa[-_][\w-]*\.json/i;
const DELIVERY = /\bagora(?:\.cmd|\.py)?\s[^\n]*\b(?:submit|done)\s+#?\d+|hub_agora_(?:task_)?(?:submit|done)/i;

const brief = (text: string, limit = 140): string => {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : flat.slice(0, limit - 1) + '…';
};

export interface Call { tool: string; command: string; ok: boolean | null }

export function judgePractices(calls: Call[]): Practice[] {
  const found = new Map<string, { state: PracticeState; evidence: string }>();
  const mark = (id: string, state: PracticeState, evidence: string) => {
    if (found.get(id)?.state === 'fail' && state !== 'fail') return;
    found.set(id, { state, evidence: brief(evidence) });
  };
  let committed = false;
  for (const { tool, command, ok } of calls) {
    const text = `${tool} ${command}`;
    if (TESTS.test(command) && ok !== null) mark('tests', ok ? 'ok' : 'fail', command);
    if (BROWSER.test(text) && ok !== false) mark('browser', 'ok', text);
    if (DOCS.test(command) && (WRITE_TOOL.test(tool) || ADD.test(command) || COMMIT.test(command))) mark('docs', 'ok', text);
    if ((ADD.test(command) || COMMIT.test(command)) && EVIDENCE.test(command)) mark('no_evidence', 'fail', command);
    if (COMMIT.test(command) && ok !== false) {
      committed = true;
      const m = AUTHOR.exec(command);
      const who = (m?.[1] || m?.[2] || m?.[3] || '').trim();
      if (TRAILER.test(command)) mark('commit', 'fail', 'trailer: ' + command);
      else if (who) mark('commit', who.toLowerCase().startsWith(EXPECTED_AUTHOR) ? 'ok' : 'fail', 'author ' + who);
      else mark('commit', 'unknown', command);
    }
    if (DELIVERY.test(text) && ok !== false) mark('agora', 'ok', text);
  }
  if (committed && !found.has('no_evidence')) mark('no_evidence', 'ok', '');
  return PRACTICES.map(([id, label]) => ({ id, label, ...(found.get(id) ?? { state: 'pending' as const, evidence: '' }) }));
}

const finished = (s: Step) => s.state !== 'running' && s.state !== 'waiting';
const stepOk = (s: Step) => s.state === 'succeeded';

function stepsOf(turn: Turn): LoopStep[] {
  const out: LoopStep[] = [];
  const thoughtsAfter = new Map<number, number>();
  for (const th of turn.thoughts ?? []) thoughtsAfter.set(th.afterStep, (thoughtsAfter.get(th.afterStep) ?? 0) + 1);
  if (thoughtsAfter.has(0) || turn.steps.length === 0 && (turn.thinking || turn.text)) out.push({ kind: 'plan', label: 'Reasoning#loop', lap: 1 });
  turn.steps.forEach((s, i) => {
    const command = s.command ?? '';
    out.push({ kind: 'tool', label: brief(s.label || `${s.tool} ${command}`), lap: s.round });
    if (finished(s)) {
      out.push({ kind: 'result', label: s.tool, lap: s.round, ok: stepOk(s) });
      if (stepOk(s) && COMMIT.test(command)) out.push({ kind: 'commit', label: brief(command), lap: s.round });
      if (stepOk(s) && DELIVERY.test(`${s.tool} ${command}`)) out.push({ kind: 'submit', label: brief(command), lap: s.round });
    }
    if (thoughtsAfter.has(i + 1)) out.push({ kind: 'plan', label: 'Reasoning#loop', lap: s.round });
  });
  if (!turn.streaming && (turn.steps.length || turn.text)) out.push({ kind: 'end', label: turn.error ? 'Turn failed' : 'Turn finished', lap: turn.rounds || 1 });
  return out.slice(-80);
}

export function loopOfTurn(turn: Turn, startedAt?: number, now = Date.now(), wroteSinceResult = true): LoopView {
  const last = turn.steps[turn.steps.length - 1];
  const running = [...turn.steps].reverse().find((s) => s.state === 'running');
  let phase: LoopPhase = 'plan';
  if (!turn.streaming) phase = 'done';
  else if (running || turn.live?.phase === 'tool') phase = 'act';
  else if (last && finished(last) && !wroteSinceResult) phase = 'observe';
  else if (turn.live?.phase === 'thinking' || turn.live?.phase === 'writing') phase = 'plan';
  // After a result the model is reading it: observe until it writes again.
  else if (last && finished(last)) phase = 'observe';
  const metrics = turn.metrics;
  const tokens = (metrics?.inputTokens ?? 0) + (metrics?.outputTokens ?? 0) || null;
  const calls: Call[] = turn.steps.map((s) => ({ tool: s.tool, command: s.command ?? s.label ?? '', ok: finished(s) ? stepOk(s) : null }));
  const begin = startedAt ?? (turn.thoughts ?? [])[0]?.startedAt;
  return {
    phase,
    lap: Math.max(1, turn.rounds, last?.round ?? 1),
    tool: phase === 'act' ? (running?.tool ?? turn.live?.label ?? '') : '',
    elapsedS: turn.streaming && begin ? Math.max(0, Math.round((now - begin) / 1000)) : metrics?.responseTime ?? null,
    tokens,
    steps: stepsOf(turn),
    practices: judgePractices(calls),
  };
}

/** A sub-agent says less: a tool in flight is act, otherwise a running worker is planning. */
export function loopOfWorker(worker: Worker, now = Date.now()): LoopView {
  const running = worker.status === 'running' || worker.status === 'queued';
  const phase: LoopPhase = !running ? 'done' : worker.toolInFlight ? 'act' : 'plan';
  const start = worker.startedLocal ?? worker.firstSeen;
  const end = worker.endedLocal ?? now;
  const tokens = (worker.inTok ?? 0) + (worker.outTok ?? 0) || null;
  return {
    phase,
    lap: Math.max(1, worker.round ?? worker.rounds ?? 1),
    tool: phase === 'act' ? worker.lastTool : '',
    elapsedS: start ? Math.max(0, Math.round((end - start) / 1000)) : null,
    tokens,
    steps: [],
    practices: [],
  };
}
