/**
 * The background monitor: while the Cookbook is open, ask the server what
 * its sessions are doing (`/api/cookbook/tasks/status`), fold that into
 * the stored tasks, and register a ready serve as a model endpoint — the
 * same loop the previous interface ran, minus the DOM.
 */
import { useEffect } from 'react';
import { advertisedEndpoint, connectHost, endpointsFor, expectedModel, getCookbookState, loadState, modelMatches, patchTask, probeEndpoint, registerEndpoint, tasksStatus, updateState } from '../../adapters/cookbook';
import { depInstallSucceeded, outputLooksReady, redactTask, type LiveStatus, type Task } from '../../lib/cookbook/tasks';
import { portOf } from '../../lib/cookbook/serve';

const registering = new Set<string>();

function applyLive(live: LiveStatus[]): { changed: boolean; ready: Task[] } {
  const byId = new Map(live.map((l) => [l.session_id, l]));
  const s = getCookbookState();
  const known = new Set(s.tasks.map((t) => t.sessionId));
  let changed = false;
  const next: Task[] = s.tasks.map((t) => ({ ...t }));
  for (const l of live) {
    if (!l.session_id || known.has(l.session_id) || s.removedTasks[l.session_id]) continue;
    const remoteHost = l.remote && l.remote !== 'local' ? l.remote : '';
    next.push(
      redactTask({
        id: l.session_id,
        sessionId: l.session_id,
        name: l.model || l.session_id,
        type: l.type || 'download',
        status: l.status === 'completed' ? 'done' : l.status || 'running',
        progress: l.progress || '',
        output: l.output_tail || '',
        ts: Date.now(),
        payload: { repo_id: l.model || l.session_id, remote_host: remoteHost, _cmd: l.cmd || '(adopted from the live session list)' },
        remoteHost,
        _adoptedExternally: true,
      }),
    );
    known.add(l.session_id);
    changed = true;
  }
  const ready: Task[] = [];
  for (const task of next) {
    const l = byId.get(task.sessionId);
    if (!l) continue;
    const combined = `${task.output || ''}\n${l.output_tail || ''}`;
    const depDone = Boolean(task.payload?._dep) && depInstallSucceeded(combined);
    const downloadDone = task.type === 'download' && combined.includes('DOWNLOAD_OK');
    const serveReady = task.type === 'serve' && (l.status === 'ready' || outputLooksReady(l.output_tail || task.output || ''));
    let status: string | null = null;
    if (depDone || downloadDone) status = 'done';
    else if (serveReady) status = 'ready';
    else if (l.status === 'completed') status = 'done';
    else if (l.status === 'error') status = 'error';
    else if (l.status === 'stopped') status = task.type === 'download' ? 'crashed' : 'stopped';
    else if (l.status === 'running' || l.status === 'ready') status = l.status;
    if (status && status !== task.status) {
      task.status = status;
      changed = true;
    }
    if (serveReady && !task._serveReady) {
      task._serveReady = true;
      changed = true;
    }
    if (l.progress && l.progress !== task.progress) {
      task.progress = l.progress;
      changed = true;
    }
    if (l.exit_code !== undefined && l.exit_code !== null && l.exit_code !== task.exit_code) {
      task.exit_code = l.exit_code;
      changed = true;
    }
    if (l.output_tail) {
      const prev = task.output || '';
      const tail = l.output_tail;
      if (!prev.endsWith(tail)) {
        task.output = `${prev ? `${prev}\n` : ''}${tail}`.slice(-6000);
        changed = true;
      }
    }
    if (l.diagnosis && !task._diagnosisDismissed && l.diagnosis.message !== task._backendDiagnosis?.message) {
      task._backendDiagnosis = l.diagnosis;
      changed = true;
    }
    if (l.cmd && !task.payload?._cmd) {
      task.payload = { ...(task.payload || {}), _cmd: l.cmd };
      changed = true;
    }
    if (task.type === 'serve' && (l.status === 'ready' || task._serveReady) && !task._endpointAdded) ready.push(task);
  }
  if (changed) updateState((st) => ({ ...st, tasks: next }));
  return { changed, ready };
}

async function registerReady(task: Task, say: (m: string) => void) {
  if (registering.has(task.sessionId)) return;
  registering.add(task.sessionId);
  try {
    let host = connectHost(task.remoteHost);
    const cmd = task.payload?._cmd || '';
    let port = portOf(cmd) || '8000';
    let baseUrl = `http://${host}:${port}/v1`;
    const adv = advertisedEndpoint(task.output || '', host);
    if (adv) ({ host, port, baseUrl } = adv);
    const image = cmd.includes('diffusion_server') || cmd.includes('mlx_image_server');
    const expected = expectedModel(task);
    const eps = await endpointsFor();
    const hostPort = `${host}:${port}`;
    const existing = eps.find((e) => e.baseUrl === baseUrl || e.baseUrl.includes(hostPort) || e.name === task.name);
    if (existing) {
      const models = existing.models || [];
      if (models.length && !models.some((m) => modelMatches(m, expected))) {
        patchTask(task.sessionId, { status: 'error', _serveReady: false, output: `${task.output || ''}\n\nPort ${hostPort} answered, but it is serving ${models.join(', ')}, not ${expected || task.name}. The new serve likely failed or the port is occupied by an older server.`.trim() });
        say(`Port ${hostPort} is serving something else`);
        return;
      }
      patchTask(task.sessionId, { _endpointAdded: true });
      if (!models.length) void probeEndpoint(existing.id);
      return;
    }
    const supportsTools = cmd.includes('--enable-auto-tool-choice') || (!image && /(?:^|\s)(?:deepseek|gpt-[45o]|claude|gemini|qwen3|qwen2\.5|mixtral|llama-[34]|minimax|kimi|hermes|glm-4)/i.test(task.name));
    const id = await registerEndpoint({ baseUrl, name: task.name, remoteHost: task.remoteHost, model: expected, image, supportsTools });
    if (id) {
      patchTask(task.sessionId, { _endpointAdded: true });
      say(`Model endpoint added: ${hostPort}`);
      void probeEndpoint(id);
    }
  } finally {
    registering.delete(task.sessionId);
  }
}

/** Poll while mounted: fast when something is live, slow otherwise. */
export function useTaskMonitor(say: (m: string) => void, enabled = true): void {
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: number | null = null;
    let inFlight = false;
    let n = 0;
    const tick = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        if (n % 6 === 0) await loadState().catch(() => null);
        n += 1;
        const live = await tasksStatus();
        if (stopped) return;
        const { ready } = applyLive(live);
        for (const task of ready) void registerReady(task, say);
      } catch {
        /* the next tick tries again */
      } finally {
        inFlight = false;
        if (!stopped) {
          const anyLive = getCookbookState().tasks.some((t) => t.status === 'running' || t.status === 'queued');
          timer = window.setTimeout(() => void tick(), anyLive ? 3000 : 12000);
        }
      }
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [enabled, say]);
}
