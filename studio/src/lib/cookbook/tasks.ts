/**
 * Cookbook tasks: the shape the server stores (`/api/cookbook/state`) and
 * the shell commands that read, stop and kill a task's session — tmux on
 * Linux/macOS/Termux, a PowerShell process tree on Windows. Ported from
 * cookbookRunning.js.
 */
import type { ServeFields } from './serve';

export type TaskType = 'serve' | 'download';
export type TaskStatus = 'queued' | 'running' | 'ready' | 'done' | 'error' | 'crashed' | 'stopped' | string;

export interface TaskPayload {
  repo_id?: string;
  remote_host?: string;
  remote_server_key?: string;
  remote_server_name?: string;
  ssh_port?: string;
  platform?: string;
  include?: string;
  backend?: string;
  local_dir?: string;
  env_prefix?: string;
  _cmd?: string;
  _fields?: ServeFields;
  _env?: string;
  _envPath?: string;
  _gpus?: string;
  _dep?: boolean;
  env_path?: string;
  [k: string]: unknown;
}

export interface Task {
  id: string;
  sessionId: string;
  name: string;
  type: TaskType;
  status: TaskStatus;
  output: string;
  progress?: string;
  ts: number;
  payload: TaskPayload | null;
  remoteHost: string;
  remoteServerKey?: string;
  remoteServerName?: string;
  sshPort?: string;
  platform?: string;
  exit_code?: number | null;
  _serveReady?: boolean;
  _endpointAdded?: boolean;
  _adoptedExternally?: boolean;
  _backendDiagnosis?: { message?: string } | null;
  _diagnosisDismissed?: boolean;
  _unreachable?: boolean;
  [k: string]: unknown;
}

/** What `/api/cookbook/tasks/status` says about one live session. */
export interface LiveStatus {
  session_id: string;
  type: TaskType;
  model: string;
  status: 'running' | 'ready' | 'completed' | 'error' | 'stopped' | 'unknown' | string;
  progress: string;
  phase?: string;
  diagnosis?: { message?: string } | null;
  output_tail?: string;
  exit_code?: number | null;
  cmd?: string;
  tps?: number | null;
  reqs?: number | null;
  pct?: number | null;
  remote?: string;
}

export const LIVE_STATUSES = new Set(['running', 'ready', 'queued']);

const sshPrefix = (port?: string) => (port && port !== '22' ? `-p ${port} ` : '');
const shQuote = (v: string) => "'" + v.replace(/'/g, "'\\''") + "'";
const REMOTE_PATH = 'PATH="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; ';

export const taskHost = (t: Task): string => t.remoteHost || t.payload?.remote_host || '';
export const taskIsWindows = (t: Task, hostPlatform: string): boolean => (taskHost(t) ? t.platform === 'windows' : (t.platform || hostPlatform) === 'windows');

function winPs(t: Task, ps: string): string {
  const command = `powershell -Command "${ps}"`;
  const host = taskHost(t);
  return host ? `ssh ${sshPrefix(t.sshPort)}${host} ${shQuote(command)}` : command;
}

function winStopTree(t: Task): string {
  const host = taskHost(t);
  const sd = host ? '$env:TEMP\\odysseus-sessions' : '$env:TEMP\\odysseus-tmux';
  const sid = t.sessionId;
  const fn = `function Stop-Tree([int]$Id) { Get-CimInstance Win32_Process -Filter ('ParentProcessId = ' + $Id) -ErrorAction SilentlyContinue | ForEach-Object { Stop-Tree ([int]$_.ProcessId) }; Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue }`;
  return host
    ? `${fn}; $p = Get-Content '${sd}\\${sid}.pid' -ErrorAction SilentlyContinue; if ($p -match '^\\d+$') { Stop-Tree ([int]$p) }; Remove-Item '${sd}\\${sid}.*' -Force -ErrorAction SilentlyContinue`
    : `${fn}; $p = Get-Content (Join-Path $env:TEMP 'odysseus-tmux\\${sid}.pid') -ErrorAction SilentlyContinue; if ($p -match '^\\d+$') { Stop-Tree ([int]$p) }; Remove-Item (Join-Path $env:TEMP 'odysseus-tmux\\${sid}.*') -Force -ErrorAction SilentlyContinue`;
}

/** Ctrl-C, two seconds, then kill the session. */
export function gracefulKillCmd(t: Task, hostPlatform: string): string {
  if (taskIsWindows(t, hostPlatform)) return winPs(t, winStopTree(t));
  const host = taskHost(t);
  const inner = `tmux send-keys -t ${t.sessionId} C-c 2>/dev/null; sleep 2; tmux kill-session -t ${t.sessionId} 2>/dev/null`;
  return host ? `ssh ${sshPrefix(t.sshPort)}${host} '${REMOTE_PATH}${inner}'` : inner;
}

/** SIGKILL the pane's process tree, then the session. */
export function forceKillCmd(t: Task, hostPlatform: string): string {
  if (taskIsWindows(t, hostPlatform)) return gracefulKillCmd(t, hostPlatform);
  const sid = t.sessionId;
  const inner = `PIDS=$(tmux list-panes -t ${sid} -F "#{pane_pid}" 2>/dev/null); if [ -n "$PIDS" ]; then for P in $PIDS; do pkill -KILL -P "$P" 2>/dev/null; kill -9 "$P" 2>/dev/null; done; fi; tmux kill-session -t ${sid} 2>/dev/null`;
  const host = taskHost(t);
  return host ? `ssh ${sshPrefix(t.sshPort)}${host} ${shQuote(REMOTE_PATH + inner)}` : inner;
}

/** The last N lines of the session's output. */
export function captureCmd(t: Task, hostPlatform: string, lines = 500): string {
  if (taskIsWindows(t, hostPlatform)) {
    const host = taskHost(t);
    const ps = host ? `Get-Content '$env:TEMP\\odysseus-sessions\\${t.sessionId}.log' -Tail ${lines} -ErrorAction SilentlyContinue` : `Get-Content (Join-Path $env:TEMP 'odysseus-tmux\\${t.sessionId}.log') -Tail ${lines} -ErrorAction SilentlyContinue`;
    return winPs(t, ps);
  }
  const host = taskHost(t);
  const inner = `tmux capture-pane -t ${t.sessionId} -p -S -${lines}`;
  return host ? `ssh ${sshPrefix(t.sshPort)}${host} '${REMOTE_PATH}${inner}' 2>/dev/null` : `${inner} 2>/dev/null`;
}

/** `echo ALIVE|DEAD` for a tmux session; null on Windows (the status route knows). */
export function aliveCmd(t: Task, hostPlatform: string): string | null {
  if (taskIsWindows(t, hostPlatform)) return null;
  const inner = `if tmux has-session -t ${t.sessionId} 2>/dev/null; then echo ALIVE; else echo DEAD; fi`;
  const host = taskHost(t);
  return host ? `ssh ${sshPrefix(t.sshPort)}${host} ${shQuote(REMOTE_PATH + inner)}` : inner;
}

/** A shell command wrapped for a task's host (or run here). */
export function onHostCmd(host: string, port: string | undefined, cmd: string): string {
  return host ? `ssh ${sshPrefix(port)}${host} ${shQuote(cmd)}` : cmd;
}

/** The "downloading 42% · 3.1 GB/s" text out of a progress line. */
export function downloadBadge(progress: string | undefined): string {
  const p = progress || '';
  const pct = p.match(/\b(\d{1,3})%/);
  const speed = p.match(/(\d+(?:\.\d+)?\s*[KMG]B\/s)/i);
  if (!pct && !speed) return '';
  return [pct ? `${pct[1]}%` : '', speed ? speed[1] : ''].filter(Boolean).join(' · ');
}

export function taskDisplayName(t: Task): string {
  const base = t.name || t.payload?.repo_id || t.sessionId;
  return base.length > 60 ? `${base.slice(0, 59)}…` : base;
}

/** The stored-state sanitiser: never keep a token in a task. */
export function redactTask(t: Task): Task {
  const out = { ...t };
  const p = out.payload ? { ...out.payload } : null;
  if (p) {
    delete p.hf_token;
    out.payload = p;
  }
  if (out.type === 'serve' && typeof out.output === 'string' && out.output.length > 5000) out.output = out.output.slice(-5000);
  return out;
}

/** Does this looks like a serve that is answering already. */
export function outputLooksReady(text: string): boolean {
  return /Application startup complete|Uvicorn running on|server is listening on|Ollama API ready|listening on http/i.test(text || '');
}

export function depInstallSucceeded(text: string): boolean {
  return /Successfully installed|Requirement already satisfied.*\n?.*(?:$)|DEP_OK|already up-to-date|already up to date/i.test(text || '') && !/ERROR:|Failed to build/i.test((text || '').slice(-2000));
}

/** Terminal escapes and carriage-return redraws out of a captured pane. */
export function stripAnsi(text: string): string {
  return (text || '')
    .replace(new RegExp('\\u001b\\[[0-9;?]*[ -/]*[@-~]', 'g'), '')
    .replace(new RegExp('\\u001b[()][A-Z0-9]', 'g'), '')
    .replace(new RegExp('[\\u0000-\\u0008\\u000b\\u000c\\u000e-\\u001f\\u007f]', 'g'), '')
    .split('\n')
    .map((line) => {
      const parts = line.split('\r');
      return parts[parts.length - 1] || parts[parts.length - 2] || '';
    })
    .join('\n');
}
