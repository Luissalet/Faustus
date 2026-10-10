/**
 * #247 — which workspace files the CURRENT turn has read and written, as a
 * pure function of the steps the turn already streams (read_file, edit_file,
 * write_file, apply_patch, grep, bash with paths…). No React, no fetch: the
 * Files tab paints the result and tests/live_tree_marks.check.mjs drives it.
 *
 * Rules:
 *  - two marks, `written` beats `read`;
 *  - only steps that are running, succeeded or partial count (a proposal that
 *    still waits for approval, a denied or failed call touched nothing);
 *  - "the current turn" = every assistant turn after the LAST user message, so
 *    a new user message clears the marks while the finished turn's marks stay
 *    until then;
 *  - paths are keyed relative to the workspace (forward slashes, case-folded on
 *    Windows) so `D:\proj\a.txt`, `./a.txt` and `a.txt` are the same file.
 */
import type { Step, Turn } from './model';

export type FileMark = 'read' | 'written';
export type FileMarks = Record<string, FileMark>;
export interface FolderMark { read: number; written: number }

const LIVE_STATES = new Set<string>(['running', 'succeeded', 'partial']);
const WRITE_TOOLS = new Set(['write_file', 'edit_file', 'create_file', 'append_file', 'apply_patch', 'str_replace', 'multi_edit']);
const READ_TOOLS = new Set(['read_file', 'view_file', 'open_file']);
const SHELL_TOOLS = new Set(['bash', 'powershell', 'shell', 'cmd']);
const PATH_KEYS = ['path', 'file_path', 'filePath', 'file', 'filename'] as const;

const isWindowsRoot = (workspace: string) => /^[A-Za-z]:[\\/]/.test(workspace) || workspace.includes('\\') || workspace.startsWith('//');

/** `a/./b/../c` → `a/c`; null when it climbs above its own start. */
function squash(parts: string[]): string[] | null {
  const out: string[] = [];
  for (const part of parts) {
    if (!part || part === '.') continue;
    if (part === '..') { if (!out.length) return null; out.pop(); continue; }
    out.push(part);
  }
  return out;
}

/**
 * The canonical key of `path` inside `workspace`, or null when it is empty,
 * outside the workspace or not a path at all. The workspace root itself is ''.
 */
export function markKey(path: unknown, workspace: string): string | null {
  if (typeof path !== 'string') return null;
  const raw = path.trim().replace(/^["']|["']$/g, '');
  if (!raw || raw.length > 4096 || /[\0\r\n]/.test(raw) || /^[a-z][a-z0-9+.-]+:\/\//i.test(raw)) return null;
  const fold = isWindowsRoot(workspace);
  const norm = raw.replace(/\\/g, '/');
  const root = workspace.replace(/\\/g, '/').replace(/\/+$/, '');
  const same = (a: string, b: string) => (fold ? a.toLowerCase() === b.toLowerCase() : a === b);
  const absolute = norm.startsWith('/') || /^[A-Za-z]:\//.test(norm);
  let rel: string[] | null;
  if (absolute) {
    if (!root) return null;
    const head = squash(root.split('/'));
    const body = squash(norm.split('/'));
    if (!head || !body || body.length < head.length) return null;
    for (let i = 0; i < head.length; i++) if (!same(head[i], body[i])) return null;
    rel = body.slice(head.length);
  } else {
    rel = squash(norm.split('/'));
  }
  if (!rel) return null;
  const key = rel.join('/');
  return fold ? key.toLowerCase() : key;
}

function jsonArgs(text: string | undefined): Record<string, unknown> | null {
  const s = (text || '').trim();
  if (!s.startsWith('{')) return null;
  try {
    const data: unknown = JSON.parse(s);
    return data && typeof data === 'object' && !Array.isArray(data) ? (data as Record<string, unknown>) : null;
  } catch { return null; }
}

function argPath(command: string | undefined): string {
  const args = jsonArgs(command);
  if (args) {
    for (const key of PATH_KEYS) if (typeof args[key] === 'string' && (args[key] as string).trim()) return args[key] as string;
    return '';
  }
  // Legacy file tools take the path on the first line.
  return (command || '').split('\n')[0].trim();
}

/** `*** Add/Update/Delete File: x` headers of an apply_patch body. */
export function patchPaths(command: string | undefined): string[] {
  const args = jsonArgs(command);
  const text = args ? String(args.patch_text ?? args.patchText ?? args.patch ?? '') : command || '';
  const paths: string[] = [];
  for (const line of text.replace(/\r\n?/g, '\n').split('\n')) {
    const m = /^\*\*\* (?:Add|Update|Delete) File: (.+)$/.exec(line);
    if (m) paths.push(m[1].trim());
  }
  return paths;
}

/** Files named by `path:line:text` lines (ripgrep) or one path per line (output of grep -l). */
export function grepPaths(output: string | undefined, limit = 200): string[] {
  const seen = new Set<string>();
  for (const line of (output || '').split('\n')) {
    const m = /^((?:[A-Za-z]:)?[^:\n]+?):\d+[:-]/.exec(line);
    if (m) seen.add(m[1].trim());
    if (seen.size >= limit) break;
  }
  return [...seen];
}

/* ── bash ── */

const READ_CMDS = new Set(['cat', 'head', 'tail', 'less', 'more', 'bat', 'nl', 'wc', 'stat', 'file', 'sort', 'uniq', 'diff', 'cmp', 'md5sum', 'sha1sum', 'sha256sum', 'type', 'get-content', 'gc', 'strings', 'xxd', 'od', 'open']);
const PATTERN_FIRST = new Set(['grep', 'egrep', 'fgrep', 'rg', 'ag', 'awk', 'sed', 'select-string']);
const WRITE_CMDS = new Set(['tee', 'touch', 'rm', 'del', 'truncate', 'set-content', 'add-content', 'out-file', 'remove-item', 'ni', 'new-item']);

function shellTokens(segment: string): string[] {
  const tokens: string[] = [];
  const re = /"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(segment))) tokens.push(m[1] ?? m[2] ?? m[3]);
  return tokens;
}

const pathLike = (tok: string) =>
  Boolean(tok) && !tok.startsWith('-') && !/[$*?|<>`(){}]/.test(tok) && !/^[a-z][a-z0-9+.-]+:\/\//i.test(tok)
  && tok !== '/dev/null' && (/[\\/]/.test(tok) || /\.[A-Za-z0-9]{1,8}$/.test(tok));

/** Best-effort file operations of a shell command line; exact enough to light a tree, never a security boundary. */
export function shellPaths(command: string | undefined): { path: string; mark: FileMark }[] {
  const ops: { path: string; mark: FileMark }[] = [];
  const text = (command || '').slice(0, 20000);
  for (const segment of text.split(/&&|\|\||;|\||\n/)) {
    const tokens = shellTokens(segment);
    const rest: string[] = [];
    for (let i = 0; i < tokens.length; i++) {
      const tok = tokens[i];
      const input = /^<(.*)$/.exec(tok);
      if (input) {
        const source = input[1] || tokens[++i] || '';
        if (pathLike(source) && !source.startsWith('<')) ops.push({ path: source, mark: 'read' });
        continue;
      }
      const redirect = /^(?:\d?>>?|&>>?)(.*)$/.exec(tok);
      if (redirect) {
        const target = redirect[1] || tokens[++i] || '';
        if (pathLike(target) && !target.startsWith('&')) ops.push({ path: target, mark: 'written' });
        continue;
      }
      rest.push(tok);
    }
    while (rest.length && (/^[A-Za-z_]\w*=/.test(rest[0]) || rest[0] === 'sudo')) rest.shift();
    const cmd = (rest.shift() || '').toLowerCase();
    if (!cmd) continue;
    const inPlace = cmd === 'sed' && rest.some((tok) => /^-[A-Za-z]*i/.test(tok) || tok.startsWith('--in-place'));
    const positional = rest.filter((tok) => !tok.startsWith('-'));
    const args = PATTERN_FIRST.has(cmd) && !rest.some((tok) => tok === '-e' || tok === '-f') ? positional.slice(1) : positional;
    if (cmd === 'cp' || cmd === 'install') {
      const dest = args.pop();
      for (const src of args) if (pathLike(src)) ops.push({ path: src, mark: 'read' });
      if (dest && pathLike(dest)) ops.push({ path: dest, mark: 'written' });
    } else if (cmd === 'mv') {
      for (const tok of args) if (pathLike(tok)) ops.push({ path: tok, mark: 'written' });
    } else if (inPlace || WRITE_CMDS.has(cmd)) {
      for (const tok of args) if (pathLike(tok)) ops.push({ path: tok, mark: 'written' });
    } else if (READ_CMDS.has(cmd) || PATTERN_FIRST.has(cmd)) {
      for (const tok of args) if (pathLike(tok)) ops.push({ path: tok, mark: 'read' });
    }
  }
  return ops;
}

/* ── steps → marks ── */

type Raw = { path: string; mark: FileMark };

/** Every raw path a single step touched, before it is keyed against the workspace. */
export function stepTouches(step: Pick<Step, 'tool' | 'command' | 'output' | 'diff' | 'state'>): Raw[] {
  if (!LIVE_STATES.has(String(step.state))) return [];
  const out: Raw[] = [];
  const tool = String(step.tool || '').toLowerCase();
  // `diff.file` is the server's display name: usually a bare basename (`main.py`), which
  // cannot be placed in the tree and would light a phantom root file. Only a real path counts.
  if (step.diff?.file && /[\\/]/.test(step.diff.file)) out.push({ path: step.diff.file, mark: 'written' });
  if (tool === 'apply_patch') {
    for (const path of patchPaths(step.command)) out.push({ path, mark: 'written' });
  } else if (WRITE_TOOLS.has(tool)) {
    out.push({ path: argPath(step.command), mark: 'written' });
  } else if (READ_TOOLS.has(tool)) {
    out.push({ path: argPath(step.command), mark: 'read' });
  } else if (tool === 'grep') {
    for (const path of grepPaths(step.output)) out.push({ path, mark: 'read' });
  } else if (SHELL_TOOLS.has(tool)) {
    const args = jsonArgs(step.command);
    out.push(...shellPaths(args ? String(args.command ?? args.cmd ?? args.script ?? '') : step.command));
  }
  return out;
}

/** Steps of the current turn: everything after the last user message. */
export function currentTurnSteps(turns: Turn[]): Step[] {
  let from = 0;
  for (let i = turns.length - 1; i >= 0; i--) if (turns[i].role === 'user') { from = i + 1; break; }
  return turns.slice(from).flatMap((turn) => (turn.role === 'assistant' ? turn.steps : []));
}

// The turn reducer replaces a step object whenever it changes, so a step's touches can be cached by identity:
// every streamed token re-renders the panel, and a grep result is 200 lines to parse.
const touchCache = new WeakMap<object, Raw[]>();

export function fileMarksFromSteps(steps: Pick<Step, 'tool' | 'command' | 'output' | 'diff' | 'state'>[], workspace: string): FileMarks {
  const marks: FileMarks = {};
  for (const step of steps) {
    let touches = touchCache.get(step);
    if (!touches) { touches = stepTouches(step); touchCache.set(step, touches); }
    for (const { path, mark } of touches) {
      const key = markKey(path, workspace);
      if (!key) continue; // empty or outside the workspace; the root itself is not a file
      if (mark === 'written' || !marks[key]) marks[key] = mark;
    }
  }
  return marks;
}

export const turnFileMarks = (turns: Turn[], workspace: string): FileMarks =>
  workspace ? fileMarksFromSteps(currentTurnSteps(turns), workspace) : {};

/** For each ancestor folder key ('' = the root), how many marked files sit below it. */
export function folderMarks(marks: FileMarks): Record<string, FolderMark> {
  const out: Record<string, FolderMark> = {};
  for (const [key, mark] of Object.entries(marks)) {
    const parts = key.split('/');
    for (let i = 0; i < parts.length; i++) {
      const dir = parts.slice(0, i).join('/');
      const slot = (out[dir] ??= { read: 0, written: 0 });
      slot[mark] += 1;
    }
  }
  return out;
}

export function markCounts(marks: FileMarks): FolderMark {
  const count: FolderMark = { read: 0, written: 0 };
  for (const mark of Object.values(marks)) count[mark] += 1;
  return count;
}

/** The mark a folder shows: written wins, then read, else none. */
export const folderMarkKind = (slot: FolderMark | undefined): FileMark | null =>
  !slot ? null : slot.written ? 'written' : slot.read ? 'read' : null;
