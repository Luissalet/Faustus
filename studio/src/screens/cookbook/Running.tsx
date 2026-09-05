import { AlertTriangle, ChevronDown, ChevronUp, Cpu, Download, ExternalLink, MoreHorizontal, Play, RefreshCw, Save, Square, Trash2, Wrench, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { Button, Dialog, EmptyState, IconButton, Menu, StatusBadge, type MenuItem, type RunStatus } from '../../components';
import { connectHost, getCookbookState, isLocal, patchTask, removeTask, sessionOutput, shellExec, updateState, useCookbookState, type Preset } from '../../adapters/cookbook';
import { diagnose, GPU_CLEANUP_COMMAND, type Diagnosis, type Fix } from '../../lib/cookbook/diagnosis';
import { addFlag, portOf, removeFlag, replaceFlag } from '../../lib/cookbook/serve';
import { captureCmd, downloadBadge, forceKillCmd, gracefulKillCmd, onHostCmd, stripAnsi, taskDisplayName, type Task } from '../../lib/cookbook/tasks';
import { t, tn } from '../../i18n';
import { launchPip, launchServe, processQueue, startDownload, targetFor } from './actions';
import { CopyButton, Uptime } from './parts';

/**
 * Running: every session the Cookbook started (or found), grouped by the
 * server it runs on, with its live phase, output and a diagnosis with
 * fixes when it fails. Stop is graceful (Ctrl-C, then kill); Kill is not.
 */

const BADGE: Record<string, RunStatus> = { queued: 'queued', running: 'running', ready: 'succeeded', done: 'succeeded', error: 'failed', crashed: 'failed', stopped: 'cancelled' };
const STATUS_TEXT: Record<string, string> = { queued: 'Queued', running: 'Running', ready: 'Ready', done: 'Done', error: 'Failed', crashed: 'Crashed', stopped: 'Stopped' };

export function Running({ say, hwBackend, onEdit, onDeps }: { say: (m: string) => void; hwBackend: string; onEdit: (task: Task, overrides?: Record<string, string>) => void; onDeps: (pkg: string) => void }) {
  const state = useCookbookState();
  const [open, setOpen] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ task: Task; kind: 'kill' | 'remove' } | null>(null);
  const [savePreset, setSavePreset] = useState<Task | null>(null);
  const [presetLabel, setPresetLabel] = useState('');

  useEffect(() => {
    const id = window.setInterval(() => void processQueue(), 6000);
    return () => window.clearInterval(id);
  }, []);

  const groups = useMemo(() => {
    const by = new Map<string, Task[]>();
    for (const task of [...state.tasks].sort((a, b) => b.ts - a.ts)) {
      const k = task.remoteServerName || task.remoteHost || 'Local';
      by.set(k, [...(by.get(k) ?? []), task]);
    }
    return [...by.entries()];
  }, [state.tasks]);

  const stop = async (task: Task, force: boolean) => {
    try {
      const cmd = force ? forceKillCmd(task, state.env.hostPlatform) : gracefulKillCmd(task, state.env.hostPlatform);
      await shellExec(cmd, 30);
      patchTask(task.sessionId, { status: 'stopped', _serveReady: false });
      say(force ? t('Killed') : t('Stopped'));
    } catch (e) {
      say((e as Error).message);
    }
  };

  const restart = async (task: Task) => {
    try {
      if (task.type === 'serve' && task.payload?._cmd) {
        const target = targetFor(state.env, task.remoteHost ? (state.env.servers.find((s) => s.host === task.remoteHost) ?? null) : null);
        await launchServe({ shortName: task.name, repo: task.payload.repo_id || task.name, cmd: task.payload._cmd, fields: task.payload._fields, target, hwBackend, replaceTaskId: task.sessionId, dep: Boolean(task.payload._dep) });
      } else if (task.type === 'download' && task.payload?.repo_id) {
        const target = targetFor(state.env, task.remoteHost ? (state.env.servers.find((s) => s.host === task.remoteHost) ?? null) : null);
        removeTask(task.sessionId);
        const out = await startDownload({ repo: task.payload.repo_id, backend: task.payload.backend === 'ollama' ? 'ollama' : task.payload.include ? 'llamacpp' : 'hf', include: task.payload.include, target, displayName: task.name });
        if ('duplicate' in out) say(t('Already downloading'));
      }
      say(t('Restarted'));
    } catch (e) {
      say((e as Error).message);
    }
  };

  const saveAsPreset = () => {
    if (!savePreset?.payload) return;
    const task = savePreset;
    const preset: Preset = { id: `p-${Date.now().toString(36)}`, label: presetLabel.trim() || task.name, repo: task.payload!.repo_id || task.name, backend: String(task.payload!._fields?.backend || ''), fields: task.payload!._fields || {}, env: task.payload!._env, envPath: task.payload!._envPath, host: task.remoteHost, ts: Date.now() };
    updateState((s) => ({ ...s, presets: [...s.presets.filter((p) => p.label !== preset.label || p.repo !== preset.repo), preset] }));
    setSavePreset(null);
    say(t('Preset saved'));
  };

  if (!state.tasks.length) {
    return <EmptyState icon={Cpu} title={t('Nothing running')} body={t('Launch a model from Models or start a download; every session shows up here with its output.')} headingLevel={3} />;
  }

  return (
    <div className="fs-ck__running" data-testid="cookbook-running">
      {groups.map(([server, tasks]) => (
        <section key={server} className="fs-ck__group">
          <h3 className="fs-ck__h">
            {server === 'Local' ? t('Local') : server} <span className="fs-muted">{tn(tasks.length, '{n} session', '{n} sessions')}</span>
          </h3>
          <ul className="fs-ck__tasks">
            {tasks.map((task) => (
              <TaskCard
                key={task.sessionId}
                task={task}
                open={open === task.sessionId}
                onToggle={() => setOpen((cur) => (cur === task.sessionId ? null : task.sessionId))}
                onStop={() => void stop(task, false)}
                onKill={() => setConfirm({ task, kind: 'kill' })}
                onRemove={() => (task.status === 'running' || task.status === 'ready' ? setConfirm({ task, kind: 'remove' }) : removeTask(task.sessionId))}
                onRestart={() => void restart(task)}
                onEdit={(o) => onEdit(task, o)}
                onDeps={onDeps}
                onSavePreset={() => {
                  setPresetLabel(task.name);
                  setSavePreset(task);
                }}
                say={say}
                hwBackend={hwBackend}
              />
            ))}
          </ul>
        </section>
      ))}

      {confirm && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setConfirm(null);
          }}
          title={confirm.kind === 'kill' ? t('Kill {name}?', { name: confirm.task.name }) : t('Remove {name}?', { name: confirm.task.name })}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setConfirm(null)} />
              <Button
                variant="danger-solid"
                size="sm"
                label={confirm.kind === 'kill' ? t('Kill') : t('Stop and remove')}
                onClick={() => {
                  const { task, kind } = confirm;
                  setConfirm(null);
                  void (async () => {
                    await stop(task, kind === 'kill');
                    if (kind === 'remove') removeTask(task.sessionId);
                  })();
                }}
              />
            </>
          }
        >
          <p className="fs-prose">{confirm.kind === 'kill' ? t('The process tree is killed without a chance to save anything.') : t('The session is stopped first, then the card goes.')}</p>
        </Dialog>
      )}

      {savePreset && (
        <Dialog
          open
          onOpenChange={(o) => {
            if (!o) setSavePreset(null);
          }}
          title={t('Save as preset')}
          description={t('The launch settings of this session, ready to apply to the same model again.')}
          footer={
            <>
              <Button variant="ghost" size="sm" label={t('Cancel')} onClick={() => setSavePreset(null)} />
              <Button variant="primary" size="sm" label={t('Save')} onClick={saveAsPreset} />
            </>
          }
        >
          <input className="fs-field" value={presetLabel} onChange={(e) => setPresetLabel(e.target.value)} aria-label={t('Preset name')} style={{ inlineSize: '100%' }} />
        </Dialog>
      )}
    </div>
  );
}

function TaskCard({ task, open, onToggle, onStop, onKill, onRemove, onRestart, onEdit, onDeps, onSavePreset, say, hwBackend }: { task: Task; open: boolean; onToggle: () => void; onStop: () => void; onKill: () => void; onRemove: () => void; onRestart: () => void; onEdit: (o?: Record<string, string>) => void; onDeps: (pkg: string) => void; onSavePreset: () => void; say: (m: string) => void; hwBackend: string }) {
  const navigate = useNavigate();
  const live = task.status === 'running' || task.status === 'ready' || task.status === 'queued';
  const [tail, setTail] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [quick, setQuick] = useState<{ cmd: string; result: string | null } | null>(null);
  const port = portOf(task.payload?._cmd || '');
  const host = connectHost(task.remoteHost);
  const diagnosis: Diagnosis | null = useMemo(() => {
    if (task._diagnosisDismissed) return null;
    const own = diagnose(`${task.output || ''}\n${tail || ''}`);
    if (own) return own;
    if (task._backendDiagnosis?.message) return { message: task._backendDiagnosis.message, fixes: [] };
    return null;
  }, [task.output, task._backendDiagnosis, task._diagnosisDismissed, tail]);

  useEffect(() => {
    if (!open) return;
    let stopped = false;
    let timer: number | null = null;
    const read = async () => {
      try {
        let text = '';
        try {
          text = await sessionOutput(task.sessionId, 400);
        } catch {
          const r = await shellExec(captureCmd(task, getCookbookState().env.hostPlatform, 400), 15);
          text = r.stdout;
        }
        if (!stopped) setTail(text || task.output || '');
      } catch {
        if (!stopped) setTail(task.output || '');
      }
      if (!stopped && live) timer = window.setTimeout(() => void read(), 4000);
    };
    void read();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [open, task.sessionId, live]); // eslint-disable-line react-hooks/exhaustive-deps

  const applyFix = async (fix: Fix) => {
    const cmd = task.payload?._cmd || '';
    const relaunch = async (next: string) => {
      setBusy(fix.label);
      try {
        const target = targetFor(getCookbookState().env, task.remoteHost ? (getCookbookState().env.servers.find((s) => s.host === task.remoteHost) ?? null) : null);
        await launchServe({ shortName: task.name, repo: task.payload?.repo_id || task.name, cmd: next, fields: task.payload?._fields, target, hwBackend, replaceTaskId: task.sessionId });
        say(t('Relaunched'));
      } catch (e) {
        say((e as Error).message);
      } finally {
        setBusy(null);
      }
    };
    switch (fix.kind) {
      case 'retry-replace':
        return relaunch(replaceFlag(cmd, fix.flag, fix.value));
      case 'retry-prepend':
        return relaunch(`${fix.value}${cmd}`);
      case 'retry-add':
        return relaunch(addFlag(cmd, fix.flag));
      case 'retry-remove':
        return relaunch(removeFlag(cmd, fix.flag));
      case 'env-fix':
        return relaunch(`${fix.value} ${cmd}`);
      case 'copy':
        await navigator.clipboard.writeText(fix.text);
        return say(t('Copied'));
      case 'copy-output':
        await navigator.clipboard.writeText(`${task.output || ''}\n${tail || ''}`.trim());
        return say(t('Copied'));
      case 'deps':
        return onDeps(fix.pkg);
      case 'edit':
        return onEdit(fix.overrides);
      case 'cpu-edit':
        return onEdit({ backend: 'llamacpp', llama_mode: 'cpu' });
      case 'field':
        return onEdit({ [fix.field]: String(fix.value) });
      case 'focus':
        return onEdit({ _focus: fix.field });
      case 'open-url':
        window.open(fix.url(`${task.output || ''}\n${tail || ''}`), '_blank', 'noopener');
        return;
      case 'clear-gpu-selection':
        updateState((s) => ({ ...s, env: { ...s.env, gpus: '' } }));
        return say(t('GPU selection cleared'));
      case 'clear-gpus':
      case 'quick-cmd': {
        const c = fix.kind === 'clear-gpus' ? GPU_CLEANUP_COMMAND : fix.cmd;
        setQuick({ cmd: c, result: null });
        const r = await shellExec(onHostCmd(task.remoteHost, task.sshPort, c), 60);
        setQuick({ cmd: c, result: `${r.exit_code === 0 ? t('Command completed.') : t('Command failed.')} (exit ${r.exit_code})\n${[r.stdout, r.stderr].filter(Boolean).join('\n').trim()}` });
        return;
      }
      case 'pip-task': {
        setBusy(fix.label);
        try {
          const target = targetFor(getCookbookState().env, task.remoteHost ? (getCookbookState().env.servers.find((s) => s.host === task.remoteHost) ?? null) : null);
          await launchPip(fix.name, fix.args, target, hwBackend);
          say(t('Started {name}', { name: fix.name }));
        } catch (e) {
          say((e as Error).message);
        } finally {
          setBusy(null);
        }
        return;
      }
      default:
        return;
    }
  };

  const serveFromDownload = () => onEdit({ _fromDownload: '1' });

  const menuItems: (MenuItem | null)[] = [{ label: t('Restart'), icon: RefreshCw, onSelect: onRestart, disabled: !task.payload }];
  if (task.type === 'serve') menuItems.push({ label: t('Edit and relaunch'), icon: Wrench, onSelect: () => onEdit() });
  if (task.type === 'serve' && task.payload?._fields) menuItems.push({ label: t('Save as preset'), icon: Save, onSelect: onSavePreset });
  if (task._endpointAdded && port) menuItems.push({ label: t('Open in Studio'), icon: ExternalLink, onSelect: () => navigate('/studio') });
  menuItems.push(null);
  if (live) menuItems.push({ label: t('Kill (force)'), icon: X, variant: 'danger', onSelect: onKill });
  menuItems.push({ label: live ? t('Stop and remove') : t('Remove'), icon: Trash2, variant: 'danger', onSelect: onRemove });

  return (
    <li className="fs-ck__task" data-status={task.status} data-open={open || undefined} data-testid="cookbook-task">
      <div className="fs-ck__task-row">
        <button type="button" className="fs-ck__task-main" onClick={onToggle} aria-expanded={open}>
          {task.type === 'serve' ? <Play size={14} aria-hidden="true" /> : <Download size={14} aria-hidden="true" />}
          <span className="fs-ck__task-name">{taskDisplayName(task)}</span>
          <StatusBadge status={BADGE[task.status] ?? 'queued'} label={t(STATUS_TEXT[task.status] ?? task.status)} />
          {task.type === 'download' && task.progress && <span className="fs-ck__task-progress">{downloadBadge(task.progress) || task.progress.slice(0, 60)}</span>}
          {task.type === 'serve' && task.progress && task.status === 'running' && <span className="fs-ck__task-progress">{task.progress}</span>}
          {task.type === 'serve' && port && <span className="fs-ck__task-port">:{port}</span>}
          {task._endpointAdded && <span className="fs-ck__task-ep">{t('endpoint')}</span>}
          {live && task.status !== 'queued' && <Uptime since={task.ts} live />}
          {open ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
        </button>
        {live && task.status !== 'queued' && <Button variant="danger" size="sm" icon={Square} label={t('Stop')} onClick={onStop} testId="cookbook-task-stop" />}
        {!live && task.type === 'download' && task.status === 'done' && !task.payload?._dep && <Button variant="primary" size="sm" icon={Play} label={t('Serve')} onClick={serveFromDownload} />}
        <Menu align="end" trigger={<IconButton icon={MoreHorizontal} label={t('Session actions')} size="sm" />} items={menuItems} />
      </div>
      {open && (
        <div className="fs-ck__task-body">
          {task.payload?._cmd && (
            <p className="fs-ck__cmd">
              <code>{task.payload._cmd}</code>
              <CopyButton text={task.payload._cmd} say={say} />
            </p>
          )}
          <p className="fs-muted">
            {task.sessionId} · {task.remoteHost ? task.remoteHost : t('local')}
            {task.type === 'serve' && port ? ` · http://${host}:${port}/v1` : ''}
            {task.exit_code !== null && task.exit_code !== undefined ? ` · exit ${task.exit_code}` : ''}
          </p>
          {diagnosis && (
            <div className="fs-ck__diag" role="status">
              <div className="fs-ck__diag-head">
                <AlertTriangle size={14} aria-hidden="true" />
                <strong>{diagnosis.message}</strong>
                <span className="fs-spacer" />
                <IconButton icon={X} size="sm" label={t('Dismiss')} onClick={() => patchTask(task.sessionId, { _diagnosisDismissed: true })} />
              </div>
              {diagnosis.suggestion && <p className="fs-ck__diag-tip">{diagnosis.suggestion}</p>}
              {diagnosis.fixes.length > 0 && (
                <div className="fs-ck__diag-fixes">
                  {diagnosis.fixes.map((fix) => (
                    <Button key={fix.label} variant={'autofix' in fix && fix.autofix ? 'primary' : 'secondary'} size="sm" label={fix.label} loading={busy === fix.label} onClick={() => void applyFix(fix)} />
                  ))}
                </div>
              )}
              {quick && (
                <pre className="fs-ck__quick">
                  <span className="fs-muted">$ {quick.cmd.split('\n')[0]}</span>
                  {'\n'}
                  {quick.result ?? t('Running…')}
                </pre>
              )}
            </div>
          )}
          <pre className="fs-ck__output" data-testid="cookbook-task-output">
            {tail === null ? t('Reading the session…') : stripAnsi(tail) || t('(no output yet)')}
          </pre>
        </div>
      )}
    </li>
  );
}
