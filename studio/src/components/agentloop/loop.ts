/**
 * Agent loop of a turn or a sub-agent: plan -> act -> observe.
 *
 * Read from what the chat already received, never from elapsed time: model
 * text or reasoning is plan, a running tool is act, a tool result with no new
 * round started yet is observe. The practice checklist depends on the kind of
 * work the turn actually did (code, research, files, operations or a plain
 * conversation, which has none) and is marked from the calls it ran and the
 * answer it wrote — same rules as src/agent_loop_state.py.
 */
import { t } from '../../i18n';
import { toolWord, type Step, type Turn, type Worker } from '../../screens/studio/model';

export type LoopPhase = 'plan' | 'act' | 'observe' | 'done' | 'idle';
export type PracticeState = 'ok' | 'fail' | 'unknown' | 'pending';
export type WorkKind = 'code' | 'research' | 'files' | 'ops' | 'chat';

export interface LoopStep {
  kind: 'plan' | 'tool' | 'result' | 'commit' | 'submit' | 'end';
  label: string;
  lap: number;
  ok?: boolean;
  /** Tool steps: the words for the tool and the one argument that says what it did. */
  title?: string;
  detail?: string;
  /** Result steps: wall time of the call. */
  ms?: number;
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
  kind: WorkKind;
  steps: LoopStep[];
  practices: Practice[];
}

export const KIND_LABEL: Record<WorkKind, string> = {
  code: 'Code#work', research: 'Research#work', files: 'Files and documents', ops: 'Operations#work', chat: 'Conversation#work',
};

export const PRACTICE_LABEL: Record<string, string> = {
  tests: 'Tests run',
  browser: 'App checked in a browser',
  docs: 'Docs updated (FAUSTUS.md / README)',
  commit: 'Commit with the right identity',
  no_evidence: 'No evidence files committed',
  agora: 'Delivered in the Agora (submit / done)',
  sources_opened: 'Sources opened, not only searched',
  sources_cited: 'Sources cited in the answer',
  dated: 'Facts carry a checked date',
  verified: 'Nothing claimed from an unseen source',
  language: "Answer in the user's language",
  output_checked: 'Output opened and checked',
  format: 'In the requested format',
  ran_ok: 'Commands finished without errors',
  checked: 'Result checked after running',
};

export const KIND_PRACTICES: Record<WorkKind, string[]> = {
  code: ['tests', 'browser', 'docs', 'commit', 'no_evidence', 'agora'],
  research: ['sources_opened', 'sources_cited', 'dated', 'verified', 'language'],
  files: ['output_checked', 'format', 'language'],
  ops: ['ran_ok', 'checked', 'language'],
  chat: [],
};

/** The code checklist, kept for callers that read it directly. */
export const PRACTICES: [string, string][] = KIND_PRACTICES.code.map((id) => [id, PRACTICE_LABEL[id]]);

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

const GIT = /(?:^|[\s;&|(])git\s+\w/i;
const AGORA_TASK = /\bagora(?:\.cmd|\.py)?\s[^\n]*\b(?:claim|submit|done|review)\s+#?\d+|hub_agora_task_/i;
const CODE_FILE = /\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|css|scss|vue|svelte|go|rs|java|kt|c|cc|cpp|h|hpp|cs|rb|php|sh|ps1|psm1|bat|cmd|sql|toml|ya?ml|json|ini|cfg|lock|gradle|swift)$/i;
const DOC_EXT = 'md|txt|docx?|odt|rtf|pdf|xlsx?|ods|csv|tsv|pptx?|odp|html?|png|jpe?g|webp|gif|svg|mp4|mp3|wav|epub';
const DOC_FILE = new RegExp(`\\.(${DOC_EXT})$`, 'i');
const DOC_OUT = new RegExp(`(?:\\s-o\\s+|--output[= ]|>\\s*|--out[= ]|save(?:fig|_as)?\\(\\s*["'])([^\\s"'>]*\\.(?:${DOC_EXT}))\\b`, 'i');
const DOC_TOOL = /create_document|edit_document|update_document|generate_image|image_gen|make_slides|export_document|render_document|save_document|write_report/i;
const WEB_TOOL = /web_?search|web_?fetch|fetch_url|open_url|search_web|searx|browse|browser|navigate/i;
const FETCH_TOOL = /web_?fetch|fetch_url|open_url|browse|browser|navigate/i;
const FETCH_CMD = /\b(curl|wget|Invoke-WebRequest|iwr|httpx?)\b[^\n]*https?:\/\//i;
const INSPECT_TOOL = /^(read|read_file|view|view_file|open_file|inspect_deliverable|inspect_media|inspect_document|preview|look_at_image)$/i;
const READ_ONLY_TOOL = /^(read|read_file|view|view_file|ls|list|list_dir|glob|grep|search_files|find|lookup_tools|todowrite|todo_write|update_plan|ask_user|search_chats|search_project_chats|recall|manage_memory)$/i;
const READ_ONLY_CMD = /^\s*(ls|dir|cat|type|head|tail|less|more|grep|rg|find|tree|wc|stat|du|df|pwd|which|where|whoami|echo|env|printenv|ps|tasklist|top|uptime|free|nvidia-smi|ping|nslookup|Get-\w+|Test-\w+|Select-String|git\s+(?:status|log|diff|show|branch|remote)|docker\s+(?:ps|images|logs|inspect)|systemctl\s+status|journalctl|kubectl\s+(?:get|describe|logs)|curl\s+(?:-s\s+)?(?:-I\s+)?https?:\/\/)\b/i;

const URL_RX = /https?:\/\/[^\s<>"'`)\]}]+/gi;
const CITE = /\[[^\]]+\]\(https?:\/\/|\[\d{1,2}\]|https?:\/\//i;
const DATE = new RegExp(
  '\\b\\d{1,2}[/.-]\\d{1,2}[/.-]\\d{2,4}\\b|\\b(19|20)\\d{2}-\\d{2}-\\d{2}\\b|' +
  '\\b\\d{1,2}\\s+(?:de\\s+)?(?:ene(?:ro)?|feb(?:rero)?|mar(?:zo)?|abr(?:il)?|may(?:o)?|jun(?:io)?|jul(?:io)?|' +
  'ago(?:sto)?|sep(?:tiembre)?|oct(?:ubre)?|nov(?:iembre)?|dic(?:iembre)?|jan(?:uary)?|february|march|april|' +
  'june|july|aug(?:ust)?|sept?(?:ember)?|october|dec(?:ember)?)\\b|' +
  '\\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2}\\b|' +
  '\\b(?:en|in|de|del|since|desde|as of|a)\\s+(?:19|20)\\d{2}\\b|\\b(?:19|20)\\d{2}\\b', 'i');
const FORMATS: Record<string, string[]> = {
  pdf: ['pdf'], docx: ['docx', 'doc'], word: ['docx', 'doc'], xlsx: ['xlsx', 'xls'], excel: ['xlsx', 'xls'],
  'hoja de cálculo': ['xlsx', 'xls', 'ods', 'csv'], spreadsheet: ['xlsx', 'xls', 'ods', 'csv'], csv: ['csv'],
  pptx: ['pptx', 'ppt'], powerpoint: ['pptx', 'ppt'], 'presentación': ['pptx', 'ppt', 'odp', 'pdf'],
  slides: ['pptx', 'ppt', 'odp', 'pdf'], markdown: ['md'], '.md': ['md'], json: ['json'], html: ['html', 'htm'],
  png: ['png'], jpg: ['jpg', 'jpeg'], jpeg: ['jpg', 'jpeg'], svg: ['svg'], txt: ['txt'], epub: ['epub'],
};
const STOP_ES = new Set(('de la que el en y los se del las un por con una su para es al lo como más pero sus le ya este sí porque esta ' +
  'entre cuando muy sin sobre también hasta hay donde quien desde todo nos durante todos uno les ni contra otros ' +
  'ese eso ante ellos esto antes algunos qué unos yo otro otras otra él tanto esa estos mucho quienes nada muchos ' +
  'cual poco ella estar estas algunas algo nosotros mi mis tú te ti tu tus ellas cómo dónde cuál puedes hazme ' +
  'dime busca buscar quiero necesito hoy está son fue ha han').split(' '));
const STOP_EN = new Set(('the of and to in is that it for on with as was are be this by at or from have an not but what all were when we ' +
  'there can your which their if will about how out up them then so some would make like into has more could than ' +
  'been who its now did get may please find show tell me give today is are does do you').split(' '));
const ARG_KEYS = ['query', 'q', 'url', 'file_path', 'path', 'filename', 'pattern', 'command', 'cmd', 'title', 'prompt',
  'description', 'name', 'task', 'text', 'input'];

const brief = (text: string, limit = 140): string => {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length <= limit ? flat : flat.slice(0, limit - 1) + '…';
};

function parseArgs(command: string): unknown {
  const text = (command || '').trim();
  if (text.startsWith('{') || text.startsWith('[')) {
    try { return JSON.parse(text); } catch { /* not JSON: the raw text is the argument */ }
  }
  return text;
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const firstLine = (s: string) => s.trim().split('\n')[0];

/** The English words for a tool; `browser_*` and `mcp__*__name` read as their last part. */
export function toolTitle(tool: string): string {
  let low = (tool || '').toLowerCase();
  if (low.startsWith('mcp__')) low = low.split('__').pop() ?? low;
  if (low.startsWith('browser_')) return `Browser · ${low.slice(8).replace(/_/g, ' ')}`;
  const word = toolWord(low);
  return word === low.replace(/_/g, ' ') ? word.charAt(0).toUpperCase() + word.slice(1) : word;
}

/** The one argument that says what a call does (its query, URL, path or command), never raw JSON. */
export function argsSummary(command: string, limit = 96): string {
  const args = parseArgs(command);
  if (isRecord(args)) {
    for (const key of ARG_KEYS) {
      let value = args[key];
      if (Array.isArray(value) && value.length && value.every((x) => typeof x === 'string')) value = value.join(' ');
      if ((typeof value === 'string' || typeof value === 'number') && String(value).trim()) return brief(firstLine(String(value)), limit);
    }
    const first = Object.values(args).find((v): v is string => typeof v === 'string' && v.trim() !== '');
    return first ? brief(firstLine(first), limit) : '';
  }
  if (Array.isArray(args)) return brief(args.map(String).join(' '), limit);
  return typeof args === 'string' && args ? brief(firstLine(args), limit) : '';
}

function target(command: string): string {
  const args = parseArgs(command);
  if (isRecord(args)) {
    for (const key of ['file_path', 'path', 'filename', 'output', 'output_path', 'target', 'title']) {
      const v = args[key];
      if (typeof v === 'string' && v.trim()) return v.trim();
    }
    return '';
  }
  if (typeof args !== 'string' || !args) return '';
  const m = /\*\*\* (?:Update|Add) File:\s*(\S+)/.exec(args);
  if (m) return m[1];
  const first = firstLine(args);
  return first && !first.includes(' ') ? first : '';
}

function shell(command: string): string {
  const args = parseArgs(command);
  if (isRecord(args)) {
    const v = args.command ?? args.cmd ?? '';
    return Array.isArray(v) ? v.map(String).join(' ') : String(v);
  }
  return typeof args === 'string' ? args : '';
}

export interface Call { tool: string; command: string; ok: boolean | null; output?: string }

function writes({ tool, command }: Call): ['code' | 'doc' | '', string] {
  if (DOC_TOOL.test(tool)) return ['doc', target(command) || tool];
  if (WRITE_TOOL.test(tool) && !READ_ONLY_TOOL.test(tool)) {
    const path = target(command);
    return [DOC_FILE.test(path) ? 'doc' : 'code', path];
  }
  const out = DOC_OUT.exec(command);
  return out ? ['doc', out[1]] : ['', ''];
}

/** Code > files > research > operations; a turn that only read or used no tool is a conversation. */
export function classify(calls: Call[]): WorkKind {
  const kinds = new Set<string>();
  for (const call of calls) {
    const [wrote, path] = writes(call);
    if (wrote === 'code' || (wrote === 'doc' && DOCS.test(path)) || GIT.test(call.command) || TESTS.test(call.command)
        || AGORA_TASK.test(`${call.tool} ${call.command}`)) kinds.add('code');
    else if (wrote === 'doc') kinds.add('files');
    else if (WEB_TOOL.test(call.tool) || FETCH_CMD.test(call.command)) kinds.add('research');
    else if (!READ_ONLY_TOOL.test(call.tool)) kinds.add('ops');
  }
  return (['code', 'files', 'research', 'ops'] as WorkKind[]).find((k) => kinds.has(k)) ?? 'chat';
}

export function language(text: string): 'es' | 'en' | null {
  const words = (text || '').toLowerCase().match(/[a-záéíóúñü']+/g)?.slice(0, 400) ?? [];
  const es = words.filter((w) => STOP_ES.has(w)).length;
  const en = words.filter((w) => STOP_EN.has(w)).length;
  if (es - en >= 2 || (es >= 1 && en === 0 && words.length >= 3)) return 'es';
  if (en - es >= 2 || (en >= 1 && es === 0 && words.length >= 3)) return 'en';
  return null;
}

const domain = (url: string): string => {
  try {
    const host = new URL(url).hostname.toLowerCase();
    return host.startsWith('www.') ? host.slice(4) : host;
  } catch { return ''; }
};

type Marks = Map<string, { state: PracticeState; evidence: string }>;

function codeMarks(calls: Call[]): Marks {
  const found: Marks = new Map();
  const mark = (id: string, state: PracticeState, evidence: string) => {
    const prev = found.get(id)?.state;
    if (prev === 'fail' && state !== 'fail') return;
    if (prev === 'ok' && state === 'unknown') return;
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
  return found;
}

function researchMarks(calls: Call[], answer: string, seenUrls: string[]): Marks {
  const found: Marks = new Map();
  const opened = calls.find((c) => c.ok === true && (FETCH_TOOL.test(c.tool) || FETCH_CMD.test(c.command)));
  if (opened) found.set('sources_opened', { state: 'ok', evidence: brief(argsSummary(opened.command)) });
  const cited = (answer.match(URL_RX) ?? []).map((u) => u.replace(/[.,;:]+$/, ''));
  const cite = CITE.exec(answer);
  if (cite) found.set('sources_cited', { state: 'ok', evidence: brief(cited[0] ?? cite[0]) });
  const date = DATE.exec(answer);
  if (date) found.set('dated', { state: 'ok', evidence: brief(answer.slice(Math.max(0, date.index - 40), date.index + date[0].length + 20)) });
  if (cited.length) {
    const known = new Set([...seenUrls, ...calls.flatMap((c) => [...(c.command.match(URL_RX) ?? []), ...((c.output ?? '').match(URL_RX) ?? [])])].map(domain));
    const unseen = cited.filter((u) => domain(u) && !known.has(domain(u)));
    found.set('verified', unseen.length
      ? { state: 'fail', evidence: brief(t('Cited but never opened or returned: {urls}', { urls: unseen.slice(0, 3).join(', ') })) }
      : { state: 'ok', evidence: t('{n} cited links seen in the tool results', { n: cited.length }) });
  } else {
    found.set('verified', { state: 'unknown', evidence: t('No links in the answer to compare with the results') });
  }
  return found;
}

function filesMarks(calls: Call[], prompt: string, skip: Set<string>): Marks {
  const found: Marks = new Map();
  const outputs: [number, string][] = [];
  calls.forEach((c, i) => {
    const [wrote, path] = writes(c);
    if (wrote && c.ok !== false) outputs.push([i, path]);
  });
  if (outputs.length) {
    const last = outputs[outputs.length - 1][0];
    const names = outputs.map(([, p]) => p.split(/[\\/]/).pop()?.toLowerCase() ?? '').filter(Boolean);
    const check = calls.slice(last + 1).find((c) => c.ok === true && (INSPECT_TOOL.test(c.tool) || names.some((n) => c.command.toLowerCase().includes(n))));
    if (check) found.set('output_checked', { state: 'ok', evidence: brief(`${toolTitle(check.tool)}: ${argsSummary(check.command)}`) });
  }
  const low = prompt.toLowerCase();
  const wanted = [...new Set(Object.entries(FORMATS)
    .filter(([word]) => new RegExp(`(?<![\\w.])${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`).test(low))
    .flatMap(([, exts]) => exts))].sort();
  if (!wanted.length) skip.add('format');
  else if (outputs.length) {
    const exts = new Set(outputs.map(([, p]) => p.includes('.') ? p.split('.').pop()!.toLowerCase() : ''));
    const hit = wanted.filter((e) => exts.has(e));
    found.set('format', hit.length
      ? { state: 'ok', evidence: '.' + hit.join(', .') }
      : { state: 'fail', evidence: brief(t('Asked for {want}; wrote {got}', { want: '.' + wanted.join(', .'), got: outputs.map(([, p]) => p).filter(Boolean).join(', ') })) });
  }
  return found;
}

function opsMarks(calls: Call[]): Marks {
  const found: Marks = new Map();
  const ended = calls.filter((c) => c.ok !== null);
  const last = ended[ended.length - 1];
  if (last) {
    const failed = ended.filter((c) => c.ok === false).length;
    const what = brief(argsSummary(last.command) || toolTitle(last.tool));
    found.set('ran_ok', last.ok ? { state: 'ok', evidence: what + (failed ? ` (${t('after {n} failed', { n: failed })})` : '') } : { state: 'fail', evidence: what });
  }
  const acting = calls.map((c, i) => (!READ_ONLY_TOOL.test(c.tool) && !READ_ONLY_CMD.test(shell(c.command)) ? i : -1)).filter((i) => i >= 0);
  if (!acting.length && ended.length) found.set('checked', { state: 'ok', evidence: t('Only commands that read') });
  else if (acting.length) {
    const check = calls.slice(acting[acting.length - 1] + 1).find((c) => c.ok === true);
    if (check) found.set('checked', { state: 'ok', evidence: brief(argsSummary(check.command) || toolTitle(check.tool)) });
  }
  return found;
}

export function judgePractices(calls: Call[], opts: { answer?: string; prompt?: string; kind?: WorkKind; seenUrls?: string[]; final?: boolean } = {}): Practice[] {
  const answer = opts.answer ?? '';
  const prompt = opts.prompt ?? '';
  const kind = opts.kind ?? classify(calls);
  const skip = new Set<string>();
  const found: Marks = kind === 'code' ? codeMarks(calls)
    : kind === 'research' ? researchMarks(calls, answer, opts.seenUrls ?? [])
      : kind === 'files' ? filesMarks(calls, prompt, skip)
        : kind === 'ops' ? opsMarks(calls) : new Map();
  if (KIND_PRACTICES[kind].includes('language')) {
    const asked = language(prompt);
    if (!asked) skip.add('language');
    else if (answer.trim()) {
      const said = language(answer);
      found.set('language', said ? { state: said === asked ? 'ok' : 'fail', evidence: `${asked} → ${said}` } : { state: 'unknown', evidence: '' });
    } else if (opts.final) found.set('language', { state: 'unknown', evidence: '' });
  }
  return KIND_PRACTICES[kind].filter((id) => !skip.has(id))
    .map((id) => ({ id, label: PRACTICE_LABEL[id], ...(found.get(id) ?? { state: 'pending' as const, evidence: '' }) }));
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
    const title = toolTitle(s.tool);
    const detail = argsSummary(command);
    out.push({ kind: 'tool', label: detail ? `${title}: ${detail}` : title, title, detail, lap: s.round });
    if (finished(s)) {
      out.push({ kind: 'result', label: title, title, lap: s.round, ok: stepOk(s), ms: s.durationMs });
      if (stepOk(s) && COMMIT.test(command)) out.push({ kind: 'commit', label: brief(command), lap: s.round });
      if (stepOk(s) && DELIVERY.test(`${s.tool} ${command}`)) out.push({ kind: 'submit', label: brief(command), lap: s.round });
    }
    if (thoughtsAfter.has(i + 1)) out.push({ kind: 'plan', label: 'Reasoning#loop', lap: s.round });
  });
  if (!turn.streaming && (turn.steps.length || turn.text)) out.push({ kind: 'end', label: turn.error ? 'Turn failed' : 'Turn finished', lap: turn.rounds || 1 });
  return out.slice(-80);
}

export function loopOfTurn(turn: Turn, startedAt?: number, now = Date.now(), wroteSinceResult = true, prompt = ''): LoopView {
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
  const calls: Call[] = turn.steps.map((s) => ({ tool: s.tool, command: s.command ?? s.label ?? '', ok: finished(s) ? stepOk(s) : null, output: s.output }));
  const begin = startedAt ?? (turn.thoughts ?? [])[0]?.startedAt;
  const kind = classify(calls);
  return {
    phase,
    lap: Math.max(1, turn.rounds, last?.round ?? 1),
    tool: phase === 'act' && running ? toolTitle(running.tool) : phase === 'act' ? (turn.live?.label ?? '') : '',
    elapsedS: turn.streaming && begin ? Math.max(0, Math.round((now - begin) / 1000)) : metrics?.responseTime ?? null,
    tokens,
    kind,
    steps: stepsOf(turn),
    practices: judgePractices(calls, {
      answer: turn.text, prompt, kind, final: !turn.streaming,
      seenUrls: (turn.sources ?? []).map((s) => s.url),
    }),
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
    tool: phase === 'act' ? toolTitle(worker.lastTool) : '',
    elapsedS: start ? Math.max(0, Math.round((end - start) / 1000)) : null,
    tokens,
    kind: 'chat',
    steps: [],
    practices: [],
  };
}
