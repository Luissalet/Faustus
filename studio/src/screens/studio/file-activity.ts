/**
 * #247 — which workspace files the CURRENT turn has read and written, as a
 * pure function of the steps the turn already streams (read_file, edit_file,
 * write_file, apply_patch, grep, bash with paths…). No React, no fetch: the
 * Files tab paints the result and tests/live_tree_marks.check.mjs drives it.
 *
 * Rules:
 *  - two marks, `written` beats `read`;
 *  - only steps that really ran count: running, succeeded or partial, and not
 *    parked on an approval card (the server closes a gated call as
 *    "succeeded" with the output «Waiting for an exact user approval.», and a
 *    denied call never runs), so a proposal touches nothing;
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
const DEAD_RESULTS = new Set<string>(['denied', 'failed', 'conflict', 'cancelled']);
/** What the agent loop answers while a gated call waits for the person (src/agent_loop.py). */
const PARKED_OUTPUT = /^Waiting for an exact user approval/i;
const WRITE_TOOLS = new Set(['write_file', 'edit_file', 'create_file', 'append_file', 'apply_patch', 'str_replace', 'multi_edit']);
const READ_TOOLS = new Set(['read_file', 'view_file', 'open_file']);
const SHELL_TOOLS = new Set(['bash', 'powershell', 'shell', 'cmd']);
const PATH_KEYS = ['path', 'file_path', 'filePath', 'file', 'filename'] as const;

const isWindowsRoot = (workspace: string) => /^(?:\\\\\?\\)?[A-Za-z]:[\\/]/.test(workspace) || workspace.includes('\\') || workspace.startsWith('//');

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

/** The home folder implied by a workspace under the usual user roots; `~/x` resolves only through it. */
function homeOf(root: string): string {
  const m = /^(\/home\/[^/]+|\/Users\/[^/]+|[A-Za-z]:\/Users\/[^/]+)(?:\/|$)/i.exec(root);
  return m ? m[1] : '';
}

/**
 * The canonical key of `path` inside `workspace`, or null when it is empty,
 * outside the workspace or not a path at all. The workspace root itself is ''.
 * Accepts `\\?\` long paths and `~/` paths that resolve inside the workspace.
 */
export function markKey(path: unknown, workspace: string): string | null {
  if (typeof path !== 'string') return null;
  let raw = path.trim().replace(/^["']|["']$/g, '');
  if (!raw || raw.length > 4096 || /[\0\r\n]/.test(raw) || /^[a-z][a-z0-9+.-]+:\/\//i.test(raw)) return null;
  raw = raw.replace(/^\\\\[?.]\\/, '').replace(/^\/\/[?.]\//, '');
  const fold = isWindowsRoot(workspace);
  let root = workspace.replace(/^\\\\[?.]\\/, '').replace(/\\/g, '/').replace(/\/+$/, '');
  let norm = raw.replace(/\\/g, '/');
  if (norm === '~' || norm.startsWith('~/')) {
    const home = homeOf(root);
    if (!home) return null;
    norm = home + norm.slice(1);
  }
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

/** `*** Add/Update/Delete File:` headers of an apply_patch body. */
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

/* ── grep ── */

/** Extension-less files that really are named in grep output (`Makefile:3:…`). */
const BARE_FILES = new Set(['makefile', 'dockerfile', 'procfile', 'rakefile', 'gemfile', 'justfile', 'license', 'readme', 'changelog', 'jenkinsfile', 'vagrantfile']);

/** A prefix of `path:line:` that can be a file: it has a dot or a separator, or is a well-known bare name — never `12`, `12:30` or `ERROR 12`. */
const plausiblePath = (p: string) =>
  p.length <= 400 && !/^\d+$/.test(p) && (/[./\\]/.test(p) && /[A-Za-z]/.test(p.replace(/^[A-Za-z]:/, '')) || BARE_FILES.has(p.toLowerCase()));

/** Files named by `path:line:text` lines (ripgrep, grep -rn). */
export function grepPaths(output: string | undefined, limit = 200): string[] {
  const seen = new Set<string>();
  for (const line of (output || '').split('\n')) {
    const m = /^((?:[A-Za-z]:)?[^:\n]+?):\d+[:-]/.exec(line);
    if (m && plausiblePath(m[1].trim())) seen.add(m[1].trim());
    if (seen.size >= limit) break;
  }
  return [...seen];
}

/* ── bash ── */

interface Tok { text: string; quoted: boolean }

/**
 * Splits a command line into segments of words. Quotes are respected (an
 * operator inside quotes is just text), here-doc bodies are skipped (they are
 * data, not commands), and `&&`, `||`, `;`, `|`, `&` and newlines end a segment.
 */
export function shellSegments(text: string): Tok[][] {
  const segments: Tok[][] = [];
  let seg: Tok[] = [];
  let cur = '';
  let quoted = false;
  let inTok = false;
  const docs: { delim: string; strip: boolean }[] = [];
  const endTok = () => { if (inTok) seg.push({ text: cur, quoted }); cur = ''; quoted = false; inTok = false; };
  const endSeg = () => { endTok(); if (seg.length) segments.push(seg); seg = []; };
  const n = text.length;
  let i = 0;
  while (i < n) {
    const ch = text[i];
    if (ch === "'") {
      inTok = true; quoted = true;
      const j = text.indexOf("'", i + 1);
      const end = j < 0 ? n : j;
      cur += text.slice(i + 1, end); i = end + 1; continue;
    }
    if (ch === '"') {
      inTok = true; quoted = true;
      let j = i + 1;
      while (j < n && text[j] !== '"') {
        if (text[j] === '\\' && text[j + 1] === '"') { cur += '"'; j += 2; continue; }
        cur += text[j]; j++;
      }
      i = j + 1; continue;
    }
    if (ch === '\n') {
      endSeg(); i++;
      // Skip the bodies of the here-docs opened on the line that just ended.
      for (const doc of docs.splice(0)) {
        while (i < n) {
          let end = text.indexOf('\n', i);
          if (end < 0) end = n;
          let line = text.slice(i, end).replace(/\r$/, '');
          i = Math.min(n, end + 1);
          if (doc.strip) line = line.replace(/^\t+/, '');
          if (line === doc.delim) break;
        }
      }
      continue;
    }
    if (ch === ' ' || ch === '\t' || ch === '\r') { endTok(); i++; continue; }
    if (ch === ';') { endSeg(); i++; continue; }
    if (ch === '&' && text[i + 1] === '&') { endSeg(); i += 2; continue; }
    if (ch === '|') { endSeg(); i += text[i + 1] === '|' ? 2 : 1; continue; }
    if (ch === '&' && text[i + 1] !== '>' && !cur.endsWith('>') && !cur.endsWith('<')) { endSeg(); i++; continue; }
    if (ch === '<' && text[i + 1] === '<') {
      if (text[i + 2] === '<') { endTok(); seg.push({ text: '<<<', quoted: false }); i += 3; continue; }
      endTok();
      i += 2;
      const strip = text[i] === '-';
      if (strip) i++;
      while (text[i] === ' ' || text[i] === '\t') i++;
      let delim = '';
      while (i < n && !/[\s;&|<>()]/.test(text[i])) {
        if (text[i] === "'" || text[i] === '"') { const q = text[i]; const j = text.indexOf(q, i + 1); const end = j < 0 ? n : j; delim += text.slice(i + 1, end); i = end + 1; continue; }
        delim += text[i]; i++;
      }
      if (delim) docs.push({ delim, strip });
      continue;
    }
    inTok = true; cur += ch; i++;
  }
  endSeg();
  return segments;
}

const READ_CMDS = new Set(['cat', 'head', 'tail', 'less', 'more', 'bat', 'nl', 'wc', 'stat', 'file', 'sort', 'uniq', 'diff', 'cmp', 'md5sum', 'sha1sum', 'sha256sum', 'type', 'get-content', 'gc', 'strings', 'xxd', 'od', 'open']);
const PATTERN_FIRST = new Set(['grep', 'egrep', 'fgrep', 'rg', 'ag', 'awk', 'sed', 'select-string']);
const WRITE_CMDS = new Set(['tee', 'touch', 'rm', 'del', 'truncate', 'set-content', 'add-content', 'out-file', 'remove-item', 'ni', 'new-item']);

/** Options that swallow the next word, per command family. */
const GREP_VALUE_FLAGS = ['-A', '-B', '-C', '-m', '-d', '-D', '-e', '-f', '--max-count', '--context', '--after-context', '--before-context', '--include', '--exclude', '--exclude-dir', '--exclude-from', '--directories', '--devices', '--label', '--color', '--colour', '--regexp', '--file', '--binary-files'];
const RG_VALUE_FLAGS = ['-A', '-B', '-C', '-m', '-e', '-f', '-g', '-t', '-T', '-j', '-M', '-r', '-E', '--glob', '--iglob', '--type', '--type-not', '--type-add', '--threads', '--max-columns', '--max-depth', '--max-filesize', '--max-count', '--replace', '--regexp', '--file', '--encoding', '--sort', '--sortr', '--color', '--colors', '--context', '--after-context', '--before-context', '--path-separator', '--ignore-file'];
const VALUE_FLAGS: Record<string, Set<string>> = {
  grep: new Set(GREP_VALUE_FLAGS), egrep: new Set(GREP_VALUE_FLAGS), fgrep: new Set(GREP_VALUE_FLAGS),
  rg: new Set(RG_VALUE_FLAGS), ag: new Set(RG_VALUE_FLAGS),
  sed: new Set(['-e', '-f', '-l', '--expression', '--file', '--line-length']),
  awk: new Set(['-F', '-v', '-f', '--field-separator', '--assign', '--file']),
};
/** Flags that supply the pattern/script themselves, so no positional word is the pattern. */
const PATTERN_FLAGS = new Set(['-e', '-f', '--regexp', '--expression', '--file']);

interface Args { positional: string[]; fileOperands: string[]; recursive: boolean; inPlace: boolean }

function parseArgs(cmd: string, rest: Tok[]): Args {
  const values = VALUE_FLAGS[cmd] ?? new Set<string>();
  const out: Args = { positional: [], fileOperands: [], recursive: false, inPlace: false };
  let patternGiven = false;
  const takeValue = (flag: string, value: string | undefined) => {
    if (PATTERN_FLAGS.has(flag)) patternGiven = true;
    if ((flag === '-f' || flag === '--file') && value) out.fileOperands.push(value);
  };
  for (let i = 0; i < rest.length; i++) {
    const { text: tok, quoted } = rest[i];
    if (quoted || !tok.startsWith('-') || tok === '-') { out.positional.push(tok); continue; }
    if (tok === '--') { for (const r of rest.slice(i + 1)) out.positional.push(r.text); break; }
    if (tok.startsWith('--')) {
      const eq = tok.indexOf('=');
      const name = eq < 0 ? tok : tok.slice(0, eq);
      if (name === '--recursive' || name === '--dereference-recursive') out.recursive = true;
      if (name === '--in-place') out.inPlace = true;
      if (values.has(name)) takeValue(name, eq < 0 ? rest[++i]?.text : tok.slice(eq + 1));
      continue;
    }
    // A cluster of short flags: `-rn`, `-A3`, `-A 3`, `-i.bak`, `-ie`.
    for (let k = 1; k < tok.length; k++) {
      const c = tok[k];
      const flag = '-' + c;
      if (cmd === 'sed' && c === 'i') { out.inPlace = true; break; } // the rest is the backup suffix
      if (values.has(flag)) {
        const attached = tok.slice(k + 1);
        takeValue(flag, attached || rest[++i]?.text);
        break;
      }
      if ((c === 'r' || c === 'R') && cmd !== 'sed' && cmd !== 'rg' && cmd !== 'ag') out.recursive = true;
    }
  }
  if (PATTERN_FIRST.has(cmd) && !patternGiven) out.positional.shift();
  return out;
}

const pathLike = (tok: string) =>
  Boolean(tok) && !tok.startsWith('-') && !/[$*?|<>`(){}]/.test(tok) && !/^[a-z][a-z0-9+.-]+:\/\//i.test(tok)
  && !/[\\/]$/.test(tok) // a trailing slash names a folder
  && tok !== '/dev/null' && (/[\\/]/.test(tok) || /\.[A-Za-z0-9]{1,8}$/.test(tok));

const hasExtension = (tok: string) => /\.[A-Za-z0-9]{1,8}$/.test(tok);
const isAbsolutePath = (p: string) => /^(?:[\\/]|[A-Za-z]:[\\/]|~(?:[\\/]|$))/.test(p);

/**
 * Best-effort file operations of a shell command line; exact enough to light a
 * tree, never a security boundary. Follows a leading `cd`, skips here-doc
 * bodies and quoted operators, and does not count folders as files.
 */
export function shellPaths(command: string | undefined): { path: string; mark: FileMark }[] {
  const ops: { path: string; mark: FileMark }[] = [];
  let cwd: string | null = '';
  const add = (raw: string, mark: FileMark) => {
    if (cwd === null) return; // after a cd we cannot place
    ops.push({ path: cwd && !isAbsolutePath(raw) ? `${cwd}/${raw}` : raw, mark });
  };
  for (const segment of shellSegments((command || '').slice(0, 20000))) {
    const rest: Tok[] = [];
    for (let i = 0; i < segment.length; i++) {
      const tok = segment[i];
      if (!tok.quoted) {
        if (tok.text === '<<<') { i++; continue; }
        const input = /^<(.*)$/.exec(tok.text);
        if (input) {
          const source = input[1] || segment[++i]?.text || '';
          if (pathLike(source)) add(source, 'read');
          continue;
        }
        const redirect = /^(?:\d?>>?|&>>?|>\|)(.*)$/.exec(tok.text);
        if (redirect) {
          const target = redirect[1] || segment[++i]?.text || '';
          if (pathLike(target) && !target.startsWith('&')) add(target, 'written');
          continue;
        }
      }
      rest.push(tok);
    }
    while (rest.length && !rest[0].quoted && (/^[A-Za-z_]\w*=/.test(rest[0].text) || rest[0].text === 'sudo')) rest.shift();
    const head = rest.shift();
    if (!head) continue;
    const cmd = head.text.replace(/^\(+/, '').toLowerCase();
    if (cmd === 'cd' || cmd === 'pushd' || cmd === 'set-location' || cmd === 'sl') {
      const dir = rest.find((tok) => tok.quoted || !tok.text.startsWith('-'))?.text;
      if (!dir || dir === '-' || /[$`~]/.test(dir)) cwd = null;
      else if (cwd !== null) cwd = isAbsolutePath(dir) ? dir : cwd ? `${cwd}/${dir}` : dir;
      continue;
    }
    const args = parseArgs(cmd, rest);
    // `grep -r foo src`, `rm -rf build`: with a recursive flag a word without an extension is a folder.
    const target = (tok: string) => pathLike(tok) && (!args.recursive || hasExtension(tok));
    for (const file of args.fileOperands) if (pathLike(file)) add(file, 'read');
    if (cmd === 'cp' || cmd === 'install') {
      const dest = args.positional.pop();
      for (const src of args.positional) if (target(src)) add(src, 'read');
      if (dest && target(dest)) add(dest, 'written');
    } else if (cmd === 'mv') {
      for (const tok of args.positional) if (target(tok)) add(tok, 'written');
    } else if (args.inPlace || WRITE_CMDS.has(cmd)) {
      for (const tok of args.positional) if (target(tok)) add(tok, 'written');
    } else if (READ_CMDS.has(cmd) || PATTERN_FIRST.has(cmd)) {
      for (const tok of args.positional) if (target(tok)) add(tok, 'read');
    }
  }
  return ops;
}

/* ── steps → marks ── */

type Raw = { path: string; mark: FileMark };
type StepLike = Pick<Step, 'tool' | 'command' | 'output' | 'diff' | 'state'> & { resultStatus?: string };

/**
 * Did this step really run? A gated call is closed by the server as
 * "succeeded" with the wait message as output (and a card asks the person); a
 * denied one never runs. Neither touched a file, whatever the step's state says.
 */
export function stepRan(step: Pick<StepLike, 'state' | 'output' | 'resultStatus'>): boolean {
  if (!LIVE_STATES.has(String(step.state))) return false;
  if (PARKED_OUTPUT.test((step.output || '').trim())) return false;
  return !DEAD_RESULTS.has(String(step.resultStatus || ''));
}

/** Every raw path a single step touched, before it is keyed against the workspace. */
export function stepTouches(step: StepLike): Raw[] {
  if (!stepRan(step)) return [];
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

export function fileMarksFromSteps(steps: StepLike[], workspace: string): FileMarks {
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

/** The directory file listings never show: any path segment that starts with a dot (the browse API omits those). */
export const isHiddenKey = (key: string) => key.split('/').some((part) => part.startsWith('.'));

/** Marks the tree can list, and how many it cannot (dot-files and folders). */
export function splitHidden(marks: FileMarks): { shown: FileMarks; hidden: number } {
  const shown: FileMarks = {};
  let hidden = 0;
  for (const [key, mark] of Object.entries(marks)) {
    if (isHiddenKey(key)) hidden += 1;
    else shown[key] = mark;
  }
  return { shown, hidden };
}

/** The mark a folder shows: written wins, then read, else none. */
export const folderMarkKind = (slot: FolderMark | undefined): FileMark | null =>
  !slot ? null : slot.written ? 'written' : slot.read ? 'read' : null;
