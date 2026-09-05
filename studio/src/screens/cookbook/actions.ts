/**
 * The three things every tab ends up doing: launch a serve, start a
 * download, run a pip/setup job. Each becomes a task the Running tab
 * follows. Ported flows from cookbookRunning.js / cookbookDownload.js.
 */
import { addTask, downloadModel, getCookbookState, isLocal, removeTask, selectedServer, serveCtx, serveModel, serverKey, serverLabel, shellExec, type CookbookEnv, type Server } from '../../adapters/cookbook';
import { activationPrefix, ggufQuant, portOf, psQuote, venvPython, type Backend, type ModelLike, type ServeFields } from '../../lib/cookbook/serve';
import { gracefulKillCmd, type Task, type TaskPayload } from '../../lib/cookbook/tasks';

export interface Target {
  server: Server | null;
  host: string;
  key: string;
  name: string;
  sshPort?: string;
  platform: string;
  env: string;
  envPath: string;
}

/** The server a launch goes to: the given one, else the picker's. */
export function targetFor(env: CookbookEnv, server?: Server | null): Target {
  const srv = server === undefined ? selectedServer(env) : server;
  const local = env.servers.find(isLocal) ?? null;
  const s = srv && !isLocal(srv) ? srv : local;
  const remote = Boolean(s && !isLocal(s));
  let e = s?.env || 'none';
  const envPath = s?.envPath || '';
  if ((!e || e === 'none') && envPath && /(?:^|\/)(?:\.?venv|env)(?:\/|$)|\/bin\/activate$/i.test(envPath)) e = 'venv';
  return { server: s, host: remote ? s!.host : '', key: remote ? serverKey(s) : 'local', name: serverLabel(s), sshPort: remote ? s!.port : undefined, platform: remote ? s!.platform : env.hostPlatform, env: e, envPath };
}

function ctxFor(env: CookbookEnv, target: Target, hwBackend: string) {
  return serveCtx(env, hwBackend, target.host ? target.server : null);
}

/** Launch a serve command as a task. Kills any task already on that port on the same host. */
export async function launchServe(input: { shortName: string; repo: string; cmd: string; fields?: ServeFields; target: Target; hwBackend: string; replaceTaskId?: string; dep?: boolean }): Promise<Task> {
  const { env } = getCookbookState();
  const { target } = input;
  const ctx = ctxFor(env, target, input.hwBackend);
  if (input.replaceTaskId) {
    const old = getCookbookState().tasks.find((t) => t.sessionId === input.replaceTaskId);
    if (old && old.type === 'serve') {
      await shellExec(gracefulKillCmd(old, env.hostPlatform), 20).catch(() => null);
      removeTask(old.sessionId);
    }
  }
  const port = portOf(input.cmd);
  if (port) {
    for (const other of getCookbookState().tasks) {
      if (other.type !== 'serve' || !other.payload?._cmd) continue;
      if (portOf(other.payload._cmd) === port && (other.remoteHost || '') === target.host) {
        await shellExec(gracefulKillCmd(other, env.hostPlatform), 20).catch(() => null);
        removeTask(other.sessionId);
      }
    }
  }
  const { sessionId } = await serveModel({
    repo_id: input.repo,
    cmd: input.cmd,
    remote_host: target.host || undefined,
    ssh_port: target.sshPort || undefined,
    env_prefix: activationPrefix({ ...ctx, env: target.env, envPath: target.envPath }) || undefined,
    gpus: env.gpus || undefined,
    platform: target.platform || undefined,
  });
  const payload: TaskPayload = { repo_id: input.repo, remote_host: target.host || undefined, remote_server_key: target.key, remote_server_name: target.name, ssh_port: target.sshPort, _cmd: input.cmd, _fields: input.fields, _env: target.env, _envPath: target.envPath, _gpus: env.gpus, _dep: input.dep || undefined };
  return addTask(sessionId, input.shortName, input.dep ? 'download' : 'serve', payload, { host: target.host, serverKey: target.key, serverName: target.name, sshPort: target.sshPort, platform: target.platform });
}

/** `python -m pip <args>` on the target, as a dependency task. */
export async function launchPip(name: string, args: string, target: Target, hwBackend: string): Promise<Task> {
  const { env } = getCookbookState();
  const ctx = { ...ctxFor(env, target, hwBackend), env: target.env, envPath: target.envPath };
  const win = target.platform === 'windows';
  const py = win ? 'python' : venvPython(ctx);
  return launchServe({ shortName: name, repo: name.replace(/\s+/g, '_'), cmd: `${py} -m pip ${args}`, target, hwBackend, dep: true });
}

/** GGUF download source for a fit row (the quant repo and its file pattern). */
export function ggufSource(model: ModelLike): { repo: string; file?: string } | null {
  const src = (model.gguf_sources ?? []).find((s) => s && s.repo);
  if (src) return { repo: src.repo!, file: src.file };
  const hay = `${model.quant_repo || ''} ${model.repo_id || ''} ${model.path || ''} ${model.name || ''}`.toLowerCase();
  if (model.is_gguf || hay.includes('gguf')) {
    const repo = model.quant_repo || model.repo_id || model.name;
    if (repo) return { repo };
  }
  return null;
}

export function ggufInclude(model: ModelLike, source: { file?: string } | null): string {
  if (source?.file) return source.file;
  if (model.quant) return `*${model.quant}*`;
  return '*.gguf';
}

/** Start a download (HF repo, GGUF pattern or Ollama tag) as a task, or queue it behind the host's current one. */
export async function startDownload(input: { repo: string; backend: 'hf' | 'ollama' | 'llamacpp'; include?: string; requiredGb?: number; target: Target; displayName?: string }): Promise<{ task: Task; queued: boolean } | { duplicate: Task }> {
  const st = getCookbookState();
  const { target } = input;
  const backend = input.backend === 'llamacpp' ? 'hf' : input.backend;
  const payload: TaskPayload & Record<string, unknown> = { repo_id: input.repo, backend };
  if (input.include) payload.include = input.include;
  if ((input.requiredGb || 0) >= 10 || input.backend === 'llamacpp') payload.disable_hf_transfer = true;
  if (target.host) {
    payload.remote_host = target.host;
    payload.remote_server_key = target.key;
    payload.remote_server_name = target.name;
    if (target.sshPort) payload.ssh_port = target.sshPort;
  }
  if (target.platform) payload.platform = target.platform;
  if (target.server?.downloadDir) payload.local_dir = target.server.downloadDir;
  const win = target.platform === 'windows';
  if (target.env === 'venv' && target.envPath) payload.env_prefix = win ? '& ' + psQuote(target.envPath.endsWith('\\Scripts\\Activate.ps1') ? target.envPath : target.envPath + '\\Scripts\\Activate.ps1') : 'source ' + (target.envPath.endsWith('/bin/activate') ? target.envPath : target.envPath + '/bin/activate');
  else if (target.env === 'conda' && target.envPath) payload.env_prefix = win ? 'conda activate ' + target.envPath : 'eval "$(conda shell.bash hook)" && conda activate ' + target.envPath;

  const shortName = (input.displayName || input.repo).split('/').pop() || input.repo;
  const part = input.include ? ggufQuant(input.include) : '';
  const taskName = part ? `${shortName} · ${part}` : shortName;
  const hostKey = target.host || 'local';
  const same = (t: Task) => t.type === 'download' && String(t.payload?.repo_id || t.name) === input.repo && (t.remoteHost || 'local') === hostKey;
  const duplicate = st.tasks.find((t) => same(t) && (t.status === 'running' || t.status === 'queued'));
  if (duplicate) return { duplicate };
  const active = st.tasks.find((t) => t.type === 'download' && (t.status === 'running' || t.status === 'queued') && (t.remoteHost || 'local') === hostKey);
  if (active) {
    const queueId = `queue-${Date.now().toString(36)}`;
    const task = addTask(queueId, taskName, 'download', payload, { host: target.host, serverKey: target.key, serverName: target.name, sshPort: target.sshPort, platform: target.platform, status: 'queued' });
    return { task, queued: true };
  }
  const sessionId = await downloadModel(payload as never);
  return { task: addTask(sessionId, taskName, 'download', payload, { host: target.host, serverKey: target.key, serverName: target.name, sshPort: target.sshPort, platform: target.platform }), queued: false };
}

/** Start the next queued download on a host once nothing is running there. */
export async function processQueue(): Promise<void> {
  const st = getCookbookState();
  const byHost = new Map<string, Task[]>();
  for (const t of st.tasks) {
    if (t.type !== 'download') continue;
    const k = t.remoteHost || 'local';
    byHost.set(k, [...(byHost.get(k) ?? []), t]);
  }
  for (const [, list] of byHost) {
    if (list.some((t) => t.status === 'running')) continue;
    const next = list.find((t) => t.status === 'queued');
    if (!next || !next.payload) continue;
    try {
      const sessionId = await downloadModel(next.payload as never);
      removeTask(next.sessionId);
      addTask(sessionId, next.name, 'download', next.payload, { host: next.remoteHost, serverKey: next.remoteServerKey, serverName: next.remoteServerName, sshPort: next.sshPort, platform: next.platform });
    } catch {
      /* try again next tick */
    }
  }
}

export function backendForDownload(backend: Backend): 'hf' | 'ollama' | 'llamacpp' {
  if (backend === 'ollama') return 'ollama';
  if (backend === 'llamacpp') return 'llamacpp';
  return 'hf';
}
