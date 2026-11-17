import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '../../components';
import { ApiError, CLIENT_API_VERSION, CLIENT_VERSION_HEADER, getJson, responseReason } from '../../adapters/api';
import { t } from '../../i18n';
import './thread-agent.css';

/**
 * Per-thread agent control plane (src/thread_agents.py, /api/threads/…):
 * who answers this thread (Faustus's own model or a CLI runner such as
 * Claude Code or Codex), "hand off to…" one task to another agent with the
 * result coming back here, and an optional git worktree for the thread with
 * its diff, commit, push and pull request.
 */

export interface ThreadAgentOption { key: string; label: string; installed: boolean; runner: boolean }
export interface ThreadJob {
  id: string; kind: 'turn' | 'handoff'; agent: string; agent_label: string; model?: string | null;
  task: string; status: 'running' | 'done' | 'failed' | 'cancelled'; ok?: boolean | null;
  error?: string | null; seconds?: number | null; started: number;
}
export interface ThreadWorktree { repo: string; path: string; branch: string; base_sha: string; base_branch: string }
export interface ThreadAgentState {
  session_id: string; agent: string; agent_label: string; model?: string | null; workspace?: string | null;
  worktree: ThreadWorktree | null; jobs: ThreadJob[]; running: ThreadJob[]; agents: ThreadAgentOption[];
}
interface WorktreeStatus extends ThreadWorktree {
  head?: string; files: { status: string; path: string }[]; commits: string[]; patch: string;
  truncated?: boolean; missing?: boolean;
}

const base = (sid: string) => `/api/threads/${encodeURIComponent(sid)}`;

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', [CLIENT_VERSION_HEADER]: CLIENT_API_VERSION },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new ApiError(await responseReason(response, path), response.status);
  return (await response.json()) as T;
}

export const loadThreadAgent = (sid: string) => getJson<ThreadAgentState>(`${base(sid)}/agent`);
export const sendThreadTurn = (sid: string, text: string) => send<ThreadJob>('POST', `${base(sid)}/turn`, { text });

/** The thread's agent, kept fresh while one of its runs is going; `onChanged`
 *  fires when a run starts or ends so the transcript can be read again. */
export function useThreadAgent(sessionId: string | null, onChanged: () => void) {
  const [state, setState] = useState<ThreadAgentState | null>(null);
  const changed = useRef(onChanged);
  changed.current = onChanged;
  const refresh = useCallback(async (): Promise<ThreadAgentState | null> => {
    if (!sessionId) { setState(null); return null; }
    try {
      const next = await loadThreadAgent(sessionId);
      setState(next);
      return next;
    } catch { setState(null); return null; }
  }, [sessionId]);
  useEffect(() => { setState(null); void refresh(); }, [refresh]);
  const running = state?.running.length ?? 0;
  useEffect(() => {
    if (!sessionId || !running) return;
    const timer = window.setInterval(() => {
      void refresh().then((next) => { if (next && next.running.length < running) changed.current(); });
    }, 2500);
    return () => window.clearInterval(timer);
  }, [sessionId, running, refresh]);
  const sendTurn = useCallback(async (sid: string, text: string) => {
    await sendThreadTurn(sid, text);
    changed.current();
    await refresh();
  }, [refresh]);
  return useMemo(() => ({ state, refresh, sendTurn, usesRunner: Boolean(state && state.agent !== 'faustus') }),
    [state, refresh, sendTurn]);
}

type Notice = (text: string, tone?: 'info' | 'warning' | 'danger') => void;

export interface ThreadAgentPanelProps {
  sessionId: string;
  state: ThreadAgentState | null;
  workspace?: string | null;
  refresh: () => Promise<unknown>;
  onChanged: () => void;
  onNotice: Notice;
}

export function ThreadAgentPanel({ sessionId, state, workspace, refresh, onChanged, onNotice }: ThreadAgentPanelProps) {
  const [agent, setAgent] = useState(state?.agent ?? 'faustus');
  const [model, setModel] = useState(state?.model ?? '');
  const [target, setTarget] = useState('');
  const [task, setTask] = useState('');
  const [handoffModel, setHandoffModel] = useState('');
  const [repo, setRepo] = useState(workspace ?? '');
  const [tree, setTree] = useState<WorktreeStatus | null>(null);
  const [commitMessage, setCommitMessage] = useState('');
  const [prTitle, setPrTitle] = useState('');
  const [busy, setBusy] = useState('');

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!state) return;
    setAgent(state.agent);
    setModel(state.model ?? '');
  }, [state?.agent, state?.model]); // eslint-disable-line react-hooks/exhaustive-deps

  const runners = (state?.agents ?? []).filter((a) => a.runner);
  useEffect(() => {
    if (!target && runners.length) setTarget((runners.find((a) => a.installed) ?? runners[0]).key);
  }, [runners.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = useCallback(async (name: string, fn: () => Promise<unknown>, done?: string) => {
    setBusy(name);
    try {
      await fn();
      if (done) onNotice(done);
    } catch (error) { onNotice((error as Error).message, 'danger'); }
    finally { setBusy(''); }
  }, [onNotice]);

  const loadTree = useCallback(async () => {
    try { setTree(await getJson<WorktreeStatus>(`${base(sessionId)}/worktree`)); }
    catch { setTree(null); }
  }, [sessionId]);
  useEffect(() => { if (state?.worktree) void loadTree(); else setTree(null); }, [state?.worktree?.path, loadTree]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="fs-thread-agent" data-testid="thread-agent-panel">
      <section aria-labelledby="ta-agent">
        <h3 id="ta-agent">{t('Agent working this thread')}</h3>
        <p className="fs-thread-agent__hint">{t('Faustus answers with the thread model. Another agent receives what you send from the composer, with the thread context, and answers here.')}</p>
        <div className="fs-thread-agent__row">
          <select aria-label={t('Agent')} value={agent} onChange={(e) => setAgent(e.target.value)} data-testid="thread-agent-select">
            {(state?.agents ?? [{ key: 'faustus', label: 'Faustus', installed: true, runner: false }]).map((a) => (
              <option key={a.key} value={a.key}>{a.label}{a.installed ? '' : ` (${t('not installed')})`}</option>
            ))}
          </select>
          {agent !== 'faustus' && <input aria-label={t('Model (optional)')} placeholder={t('Model (optional)')} value={model} onChange={(e) => setModel(e.target.value)} />}
          <Button size="sm" disabled={Boolean(busy)} testId="thread-agent-apply"
            onClick={() => void act('agent', async () => {
              await send('PUT', `${base(sessionId)}/agent`, { agent, model: model || null, workspace: agent === 'faustus' ? null : (workspace || null) });
              await refresh();
            }, t('Agent changed'))} label={t('Use in this thread')} />
        </div>
      </section>

      <section aria-labelledby="ta-handoff">
        <h3 id="ta-handoff">{t('Hand off to…')}</h3>
        <div className="fs-thread-agent__row">
          <select aria-label={t('Agent')} value={target} onChange={(e) => setTarget(e.target.value)} data-testid="thread-handoff-agent">
            {runners.map((a) => <option key={a.key} value={a.key}>{a.label}{a.installed ? '' : ` (${t('not installed')})`}</option>)}
          </select>
          <input aria-label={t('Model (optional)')} placeholder={t('Model (optional)')} value={handoffModel} onChange={(e) => setHandoffModel(e.target.value)} />
        </div>
        <textarea aria-label={t('Task')} placeholder={t('What should it do? It receives a summary of this thread.')} rows={3}
          value={task} onChange={(e) => setTask(e.target.value)} data-testid="thread-handoff-task" />
        <Button size="sm" disabled={Boolean(busy) || !task.trim() || !target} testId="thread-handoff-send"
          onClick={() => void act('handoff', async () => {
            await send('POST', `${base(sessionId)}/handoff`, { agent: target, task, model: handoffModel || null });
            setTask('');
            onChanged();
            await refresh();
          }, t('Task handed off; the result will appear in this thread'))} label={t('Hand off')} />
      </section>

      {Boolean(state?.jobs.length) && (
        <section aria-labelledby="ta-jobs">
          <h3 id="ta-jobs">{t('Runs in this thread')}</h3>
          <ul className="fs-thread-agent__jobs" data-testid="thread-agent-jobs">
            {[...(state?.jobs ?? [])].reverse().slice(0, 8).map((job) => (
              <li key={job.id} data-status={job.status}>
                <span className="fs-thread-agent__job-agent">{job.agent_label}</span>
                <span>{job.kind === 'handoff' ? t('hand-off') : t('turn')}</span>
                <span className="fs-thread-agent__job-status">{t(job.status)}{job.seconds ? ` · ${Math.round(job.seconds)} s` : ''}</span>
                {job.status === 'running' && <Button size="sm" variant="ghost"
                  onClick={() => void act('cancel', async () => { await send('POST', `${base(sessionId)}/jobs/${job.id}/cancel`); await refresh(); })} label={t('Cancel')} />}
                {job.error && <span className="fs-thread-agent__job-error" title={job.error}>{job.error}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-labelledby="ta-tree">
        <h3 id="ta-tree">{t('Thread worktree')}</h3>
        {!state?.worktree ? (
          <>
            <p className="fs-thread-agent__hint">{t('A branch of its own for this thread: the agents work in it and you see its diff here.')}</p>
            <div className="fs-thread-agent__row">
              <input aria-label={t('Repository folder')} placeholder={t('Repository folder')} value={repo} onChange={(e) => setRepo(e.target.value)} data-testid="thread-worktree-repo" />
              <Button size="sm" disabled={Boolean(busy) || !repo.trim()} testId="thread-worktree-create"
                onClick={() => void act('tree', async () => { await send('POST', `${base(sessionId)}/worktree`, { repo }); await refresh(); }, t('Worktree created'))} label={t('Create worktree')} />
            </div>
          </>
        ) : (
          <>
            <p className="fs-thread-agent__tree-head" data-testid="thread-worktree-branch"><code>{state.worktree.branch}</code> <span title={state.worktree.path}>{state.worktree.path}</span></p>
            {tree && (
              <>
                <p className="fs-thread-agent__hint">{tree.files.length ? t('{n} changed files', { n: tree.files.length }) : t('No uncommitted changes')}{tree.commits.length ? ` · ${t('{n} commits on the branch', { n: tree.commits.length })}` : ''}</p>
                {tree.patch && <pre className="fs-thread-agent__diff" data-testid="thread-worktree-diff">{tree.patch}{tree.truncated ? `\n… ${t('(truncated)')}` : ''}</pre>}
              </>
            )}
            <div className="fs-thread-agent__row">
              <input aria-label={t('Commit message')} placeholder={t('Commit message')} value={commitMessage} onChange={(e) => setCommitMessage(e.target.value)} data-testid="thread-worktree-message" />
              <Button size="sm" disabled={Boolean(busy) || !commitMessage.trim()} testId="thread-worktree-commit"
                onClick={() => void act('commit', async () => { await send('POST', `${base(sessionId)}/worktree/commit`, { message: commitMessage }); setCommitMessage(''); await loadTree(); }, t('Committed'))} label={t('Commit')} />
            </div>
            <div className="fs-thread-agent__row">
              <Button size="sm" variant="ghost" disabled={Boolean(busy)} onClick={() => void loadTree()} testId="thread-worktree-refresh" label={t('Refresh diff')} />
              <Button size="sm" variant="ghost" disabled={Boolean(busy)} testId="thread-worktree-push"
                onClick={() => void act('push', async () => { await send('POST', `${base(sessionId)}/worktree/push`); }, t('Branch pushed'))} label={t('Push')} />
            </div>
            <div className="fs-thread-agent__row">
              <input aria-label={t('Pull request title')} placeholder={t('Pull request title')} value={prTitle} onChange={(e) => setPrTitle(e.target.value)} />
              <Button size="sm" variant="ghost" disabled={Boolean(busy) || !prTitle.trim()} testId="thread-worktree-pr"
                onClick={() => void act('pr', async () => {
                  const out = await send<{ url?: string }>('POST', `${base(sessionId)}/worktree/pr`, { title: prTitle });
                  onNotice(out.url ? t('Pull request: {url}', { url: out.url }) : t('Pull request opened'));
                })} label={t('Open PR')} />
            </div>
            <Button size="sm" variant="ghost" disabled={Boolean(busy)} testId="thread-worktree-remove"
              onClick={() => void act('remove', async () => { await send('DELETE', `${base(sessionId)}/worktree`); await refresh(); }, t('Worktree removed; the branch stays'))} label={t('Remove worktree')} />
          </>
        )}
      </section>
    </div>
  );
}
