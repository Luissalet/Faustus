import { ApiError, responseReason } from './api';

/**
 * OBJ-4 / Lote 81 — Source control (`/api/git/*`, contract in
 * scratchpad/CONTRATO_GIT.md).
 *
 * Repos found under the owner's linked project folders — including nested
 * ones — with the same shape VS Code's Source Control view works from:
 * branch + ahead/behind, staged/unstaged/untracked changes, the commit
 * graph and a commit's files+diff. Reading needs only a signed-in user;
 * every mutation (checkout, stage, commit, fetch/pull/push/sync, discard)
 * needs a human at the keyboard, enforced server-side — this adapter only
 * shapes the calls and surfaces what the server said no to.
 *
 * Errors carry more than a message: `git ausente` is 503, a rejected push
 * or a dirty checkout is 409, a bad request is 400 — always with a
 * `detail` and usually an `error_class` (`git.dirty`, `git.diverged`,
 * `git.rejected`, `git.nothing_to_commit`, `git.no_identity`,
 * `dependency.missing`) plus whatever extra the case needs (`dirty`
 * paths, `ahead`/`behind`, `stderr`). `GitApiError` keeps that payload
 * instead of throwing it away the way a plain `ApiError` would, because
 * "push rejected" and "nothing to commit" need different screens, not
 * the same generic toast.
 */

export interface GitRemote {
  name: string;
  fetch_url: string;
  push_url: string;
}

export interface GitDirtyCounts {
  staged: number;
  unstaged: number;
  untracked: number;
}

export interface GitUser {
  name: string;
  email: string;
}

/**
 * OBJ-4 / Lote 83 (contract in scratchpad/CONTRATO_GIT_2.md) — SSH identity
 * chip/selector, "New repository", branch-creation dialog and the agent's
 * git policy (global + per-repo), all additive to lote 81's shapes above.
 */
export type GitIdentitySource = 'ssh_config' | 'manual';

export interface GitIdentity {
  id: string;
  label: string;
  ssh_host: string | null;
  hostname: string;
  identity_file: string;
  git_user_name: string | null;
  git_user_email: string | null;
  github_login: string | null;
  source: GitIdentitySource;
}

export interface GitIdentitiesResponse {
  identities: GitIdentity[];
  ssh_config_path: string;
  ssh_dir: string;
}

export interface GitIdentityProbeResult {
  github_login: string | null;
  ok: boolean;
  detail: string;
}

export interface RepoGitUser {
  name: string;
  email: string;
  scope: 'local' | 'global' | 'none';
}

export interface RepoIdentity {
  active: GitIdentity | null;
  remote: string;
  remote_url: string;
  git_user: RepoGitUser;
}

export interface RepoIdentityResult extends RepoIdentity {
  repo: GitRepo;
}

export interface GitFolder {
  path: string;
  project_id: string | null;
  project_name: string | null;
}

export interface GitFoldersResponse {
  folders: GitFolder[];
}

/** `DATA_DIR/git_repo_policies.json` shape and the global default under
 *  `agent_git_policy` in `src/settings.py` — identical either way. */
export interface AgentGitPolicy {
  use_branch: boolean;
  branch_prefix: string;
  commit: boolean;
  commit_message_prefix: string;
  push: boolean;
  push_set_upstream: boolean;
}

export interface RepoPolicyResponse {
  effective: AgentGitPolicy;
  overridden: boolean;
}

export interface GitRepo {
  id: string;
  path: string;
  name: string;
  project_id: string | null;
  project_name: string | null;
  root_folder: string;
  parent_repo_id: string | null;
  branch: string | null;
  detached: boolean;
  head_sha: string;
  upstream: string | null;
  ahead: number;
  behind: number;
  dirty: GitDirtyCounts;
  user: GitUser;
  remotes: GitRemote[];
  /** Lote 83: the identity whose ssh host alias matches `origin`'s remote,
   *  `null` on https/no match. Optional — a server that predates this lot
   *  simply omits it, and the identity chip then falls back to fetching
   *  `GET /repos/{id}/identity` itself. */
  identity?: { id: string; label: string; github_login: string | null } | null;
  policy?: RepoPolicyResponse;
}

export interface GitReposResponse {
  repos: GitRepo[];
  git_version: string | null;
}

export type GitFileStatusCode = 'A' | 'M' | 'D' | 'R' | 'C';

export interface GitStatusFile {
  path: string;
  status: GitFileStatusCode;
  old_path?: string;
}

export interface GitUnstagedFile {
  path: string;
  status: 'M' | 'D';
}

export interface GitUntrackedFile {
  path: string;
}

export interface GitStatus {
  branch: string | null;
  detached: boolean;
  ahead: number;
  behind: number;
  upstream: string | null;
  staged: GitStatusFile[];
  unstaged: GitUnstagedFile[];
  untracked: GitUntrackedFile[];
  conflicts: GitUntrackedFile[];
}

export interface GitCommit {
  sha: string;
  short: string;
  parents: string[];
  author: string;
  email: string;
  date: string;
  message: string;
  body: string;
  refs: string[];
}

export interface GitLogResponse {
  commits: GitCommit[];
  next_cursor: string | null;
}

export interface GitLogOptions {
  limit?: number;
  cursor?: string;
  ref?: string;
}

export interface GitBranchLocal {
  name: string;
  sha: string;
  upstream: string | null;
  ahead: number;
  behind: number;
  is_current: boolean;
}

export interface GitBranchRemote {
  name: string;
  sha: string;
}

export interface GitBranchesResponse {
  current: string | null;
  local: GitBranchLocal[];
  remote: GitBranchRemote[];
}

export interface GitCommitFile {
  path: string;
  status: GitFileStatusCode;
  additions: number | null;
  deletions: number | null;
  old_path?: string;
}

export interface GitCommitDetail {
  sha: string;
  short: string;
  author: string;
  email: string;
  date: string;
  message: string;
  body: string;
  parents: string[];
  files: GitCommitFile[];
}

export interface GitDiff {
  path: string;
  diff: string;
  truncated: boolean;
  binary: boolean;
}

export interface GitMutationResult {
  ok: boolean;
  repo: GitRepo;
}

export interface GitCheckoutResult extends GitMutationResult {
  branch: string;
}

export interface GitFetchResult extends GitMutationResult {
  output: string;
}

export interface GitPullPushResult extends GitMutationResult {
  output?: string;
}

export interface GitSyncResult extends GitMutationResult {
  pull?: GitPullPushResult;
  push?: GitPullPushResult;
}

export interface GitCommitResult extends GitMutationResult {
  sha: string;
  short: string;
  message: string;
}

/**
 * A mutation error's whole JSON body, kept alongside the message a plain
 * `ApiError` would show — the caller needs `error_class` to pick the right
 * follow-up (offer "force checkout"? show the diverge counts? the
 * rejection stderr?) and the raw fields to render them, not just prose.
 */
export class GitApiError extends ApiError {
  readonly errorClass: string | null;
  readonly payload: Record<string, unknown>;

  constructor(message: string, status: number, errorClass: string | null, payload: Record<string, unknown>) {
    super(message, status);
    this.name = 'GitApiError';
    this.errorClass = errorClass;
    this.payload = payload;
  }
}

async function payloadOf(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.clone().json();
    return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const payload = await payloadOf(response);
    const errorClass = typeof payload.error_class === 'string' ? payload.error_class : null;
    throw new GitApiError(await responseReason(response, path), response.status, errorClass, payload);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function getGit<T>(path: string): Promise<T> {
  return request<T>(path);
}

function postGit<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
}

function putGit<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function deleteGit(path: string): Promise<void> {
  return request<void>(path, { method: 'DELETE' });
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const parts = Object.entries(params).filter(([, v]) => v !== undefined && v !== '');
  if (!parts.length) return '';
  return `?${parts.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`).join('&')}`;
}

export function listRepos(projectId?: string): Promise<GitReposResponse> {
  return getGit(`/api/git/repos${query({ project_id: projectId })}`);
}

export function getRepo(repoId: string): Promise<GitRepo> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}`);
}

export function getStatus(repoId: string): Promise<GitStatus> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}/status`);
}

export function getLog(repoId: string, opts: GitLogOptions = {}): Promise<GitLogResponse> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}/log${query({ limit: opts.limit, cursor: opts.cursor, ref: opts.ref })}`);
}

export function getBranches(repoId: string): Promise<GitBranchesResponse> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}/branches`);
}

export function getCommit(repoId: string, sha: string): Promise<GitCommitDetail> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}/commits/${encodeURIComponent(sha)}`);
}

export function getCommitDiff(repoId: string, sha: string, path: string): Promise<GitDiff> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}/commits/${encodeURIComponent(sha)}/diff${query({ path })}`);
}

export function getWorkingDiff(repoId: string, path: string, staged: boolean): Promise<GitDiff> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}/diff${query({ path, staged: staged ? 1 : 0 })}`);
}

export function checkout(repoId: string, opts: { branch: string; create?: boolean; startPoint?: string }): Promise<GitCheckoutResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/checkout`, {
    branch: opts.branch,
    create: opts.create ?? false,
    start_point: opts.startPoint,
  });
}

export function createBranch(repoId: string, opts: { name: string; startPoint?: string; checkout?: boolean }): Promise<GitMutationResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/branches`, {
    name: opts.name,
    start_point: opts.startPoint,
    checkout: opts.checkout ?? true,
  });
}

export function fetchRemote(repoId: string, opts: { remote?: string; prune?: boolean } = {}): Promise<GitFetchResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/fetch`, { remote: opts.remote, prune: opts.prune ?? false });
}

export function pull(repoId: string, opts: { remote?: string; branch?: string } = {}): Promise<GitPullPushResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/pull`, opts);
}

export function push(repoId: string, opts: { remote?: string; branch?: string; setUpstream?: boolean } = {}): Promise<GitPullPushResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/push`, {
    remote: opts.remote,
    branch: opts.branch,
    set_upstream: opts.setUpstream ?? false,
    force: false,
  });
}

export function sync(repoId: string): Promise<GitSyncResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/sync`);
}

export function stage(repoId: string, paths: string[] | 'all'): Promise<GitMutationResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/stage`, paths === 'all' ? { all: true } : { paths });
}

export function unstage(repoId: string, paths: string[] | 'all'): Promise<GitMutationResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/unstage`, paths === 'all' ? { all: true } : { paths });
}

export function discard(repoId: string, paths: string[]): Promise<GitMutationResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/discard`, { paths, confirm: true });
}

export function commit(repoId: string, message: string, amend = false): Promise<GitCommitResult> {
  return postGit(`/api/git/repos/${encodeURIComponent(repoId)}/commit`, { message, amend });
}

/* ────────────────── Identities, folders, create/clone, policy ──────────────────
 * Lote 83: everything the SSH identity chip, "New repository" dialog and the
 * agent's git-policy cards call. Response shapes not fully pinned down by the
 * contract (identity create/delete) are read defensively — see `unwrap*`
 * below — so a shape the backend lot settles on slightly differently still
 * works without a follow-up change here. */

function unwrapIdentity(body: unknown): GitIdentity {
  const obj = body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
  const inner = obj.identity && typeof obj.identity === 'object' ? obj.identity : obj;
  return inner as GitIdentity;
}

export function listIdentities(): Promise<GitIdentitiesResponse> {
  return getGit('/api/git/identities');
}

export function createIdentity(opts: {
  label: string;
  sshHost?: string;
  hostname?: string;
  identityFile: string;
  gitUserName?: string;
  gitUserEmail?: string;
  writeSshConfig?: boolean;
}): Promise<GitIdentity> {
  return postGit<unknown>('/api/git/identities', {
    label: opts.label,
    ssh_host: opts.sshHost || undefined,
    hostname: opts.hostname || 'github.com',
    identity_file: opts.identityFile,
    git_user_name: opts.gitUserName || undefined,
    git_user_email: opts.gitUserEmail || undefined,
    write_ssh_config: opts.writeSshConfig ?? false,
  }).then(unwrapIdentity);
}

export function deleteIdentity(id: string): Promise<void> {
  return deleteGit(`/api/git/identities/${encodeURIComponent(id)}`);
}

export function probeIdentity(id: string): Promise<GitIdentityProbeResult> {
  return postGit(`/api/git/identities/${encodeURIComponent(id)}/probe`);
}

export function getRepoIdentity(repoId: string, remote = 'origin'): Promise<RepoIdentity> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}/identity${query({ remote })}`);
}

export function setRepoIdentity(
  repoId: string,
  opts: { identityId: string; remote?: string; setGitUser?: boolean },
): Promise<RepoIdentityResult> {
  return putGit(`/api/git/repos/${encodeURIComponent(repoId)}/identity`, {
    identity_id: opts.identityId,
    remote: opts.remote ?? 'origin',
    set_git_user: opts.setGitUser ?? true,
  });
}

export function listFolders(): Promise<GitFoldersResponse> {
  return getGit('/api/git/folders');
}

export interface CreateRepoOptions {
  mode: 'init' | 'clone';
  parentFolder: string;
  name: string;
  url?: string;
  identityId?: string;
  initialCommit?: boolean;
  defaultBranch?: string;
}

export interface CreateRepoResult {
  repo: GitRepo;
}

export function createRepo(opts: CreateRepoOptions): Promise<CreateRepoResult> {
  return postGit('/api/git/repos', {
    mode: opts.mode,
    parent_folder: opts.parentFolder,
    name: opts.name,
    url: opts.mode === 'clone' ? opts.url : undefined,
    identity_id: opts.identityId || undefined,
    initial_commit: opts.initialCommit ?? true,
    default_branch: opts.defaultBranch || 'main',
  });
}

export function getGlobalPolicy(): Promise<AgentGitPolicy> {
  return getGit('/api/git/policy');
}

export function setGlobalPolicy(policy: AgentGitPolicy): Promise<AgentGitPolicy> {
  return putGit('/api/git/policy', policy);
}

export function getRepoPolicy(repoId: string): Promise<RepoPolicyResponse> {
  return getGit(`/api/git/repos/${encodeURIComponent(repoId)}/policy`);
}

export function setRepoPolicy(repoId: string, policy: AgentGitPolicy | { inherit: true }): Promise<RepoPolicyResponse> {
  return putGit(`/api/git/repos/${encodeURIComponent(repoId)}/policy`, policy);
}

/* ────────────────────────── Pure helpers ──────────────────────────
 * No DOM, no fetch: exercised directly by
 * `studio/checks/l81-source-control.check.mjs` and reused by the screen. */

/** The commit box lights up only with something to say and something
 *  staged to say it about — the same rule VS Code enforces client-side. */
export function canCommit(message: string, stagedCount: number): boolean {
  return message.trim().length > 0 && stagedCount > 0;
}

/** "↓2 ↑1" — behind first (what you'd pull), then ahead (what you'd push);
 *  empty when the branch is caught up, not "↓0 ↑0". */
export function aheadBehindLabel(ahead: number, behind: number): string {
  const parts: string[] = [];
  if (behind > 0) parts.push(`↓${behind}`);
  if (ahead > 0) parts.push(`↑${ahead}`);
  return parts.join(' ');
}

export type GitRefKind = 'head' | 'branch' | 'remote' | 'tag';

export interface GitRefChip {
  kind: GitRefKind;
  label: string;
}

/**
 * `--decorate=full` parsed server-side to short refs, e.g.
 * `["HEAD -> master", "origin/master", "tag: v1"]`. Split each entry into
 * one chip per name so "HEAD -> master" draws as two chips (the pointer
 * and the branch it points to, both worth their own tone) instead of one
 * chip with an arrow glued into the label.
 */
export function parseRefs(refs: string[]): GitRefChip[] {
  const chips: GitRefChip[] = [];
  for (const raw of refs) {
    const entry = raw.trim();
    if (!entry) continue;
    if (entry.startsWith('tag:')) {
      const label = entry.slice(4).trim();
      if (label) chips.push({ kind: 'tag', label });
      continue;
    }
    const arrow = entry.split(' -> ');
    if (arrow.length === 2) {
      chips.push({ kind: 'head', label: arrow[0].trim() });
      chips.push(...parseRefs([arrow[1]]));
      continue;
    }
    if (entry === 'HEAD') {
      chips.push({ kind: 'head', label: entry });
    } else if (entry.includes('/')) {
      chips.push({ kind: 'remote', label: entry });
    } else {
      chips.push({ kind: 'branch', label: entry });
    }
  }
  return chips;
}

export interface GraphRow {
  sha: string;
  /** The lane this commit's own dot is drawn in. */
  lane: number;
  /** True when a lane was already waiting for this exact sha — draw the
   *  line from the row above down into this dot. False means the lane was
   *  free (or brand new) right here: nothing to connect above the dot. */
  continuesFromAbove: boolean;
  /** Lanes that merely pass through this row (another line's ancestry,
   *  unrelated to this commit) — drawn as a straight vertical segment. */
  passthrough: number[];
  /** For each parent: which lane the line from this dot travels to. The
   *  first parent always continues this commit's own lane; extra parents
   *  (a merge) each get their own lane, reused if something is already
   *  waiting for that exact parent. */
  parentLanes: { sha: string; lane: number }[];
  /** Lanes in use at this row, for sizing the SVG column. */
  laneCount: number;
}

/**
 * The graph's lane assignment (UI-020's "grafo estilo VS Code Graph"):
 * one column of dots, a line down to each commit's parent(s), extra
 * branches opening new lanes rather than crossing an existing one.
 *
 * `commits` is assumed newest-first (the server's `--date-order`, so a
 * commit is always listed before the parents it names) — the algorithm is
 * a single forward pass with no lookahead: `active[lane]` names the sha a
 * lane is currently waiting for, so a commit either lands in the lane
 * that was waiting for it or opens the first free one, and it then leaves
 * its own lane waiting for its first parent (extra parents open — or
 * join — lanes of their own for the merge lines).
 */
export function computeGraphLanes(commits: GitCommit[]): GraphRow[] {
  const active: (string | null)[] = [];
  const rows: GraphRow[] = [];

  for (const commit of commits) {
    let lane = active.indexOf(commit.sha);
    const continuesFromAbove = lane !== -1;
    if (lane === -1) {
      lane = active.indexOf(null);
      if (lane === -1) {
        active.push(null);
        lane = active.length - 1;
      }
    }
    const passthrough = active
      .map((sha, index) => (index !== lane && sha !== null ? index : -1))
      .filter((index) => index >= 0);

    const parentLanes: { sha: string; lane: number }[] = [];
    const parents = commit.parents;
    if (parents.length === 0) {
      active[lane] = null;
    } else {
      active[lane] = parents[0];
      parentLanes.push({ sha: parents[0], lane });
      for (const parentSha of parents.slice(1)) {
        let mergeLane = active.indexOf(parentSha);
        if (mergeLane === -1) {
          mergeLane = active.indexOf(null);
          if (mergeLane === -1) {
            active.push(parentSha);
            mergeLane = active.length - 1;
          } else {
            active[mergeLane] = parentSha;
          }
        }
        parentLanes.push({ sha: parentSha, lane: mergeLane });
      }
    }

    rows.push({ sha: commit.sha, lane, continuesFromAbove, passthrough, parentLanes, laneCount: active.length });
  }
  return rows;
}

export interface UnifiedDiffLine {
  type: 'add' | 'del' | 'context' | 'hunk' | 'meta';
  text: string;
}

/**
 * A unified diff's text, one typed line per row, for a plain +/− render
 * (SourceControl's `DiffPane`). Deliberately not a re-diff of two texts —
 * the server already ran `git diff`, and re-computing it from scratch
 * (the way `DiffView.tsx` does for two arbitrary texts) would sometimes
 * disagree with what git itself reported changed.
 */
export function parseUnifiedDiff(diffText: string): UnifiedDiffLine[] {
  if (!diffText) return [];
  const lines = diffText.split('\n');
  if (lines.length && lines[lines.length - 1] === '' && diffText.endsWith('\n')) lines.pop();
  return lines.map((line): UnifiedDiffLine => {
    if (line.startsWith('@@')) return { type: 'hunk', text: line };
    if (
      line.startsWith('diff ') ||
      line.startsWith('index ') ||
      line.startsWith('+++') ||
      line.startsWith('---') ||
      line.startsWith('new file') ||
      line.startsWith('deleted file') ||
      line.startsWith('similarity index') ||
      line.startsWith('rename ') ||
      line.startsWith('Binary files')
    ) {
      return { type: 'meta', text: line };
    }
    if (line.startsWith('+')) return { type: 'add', text: line.slice(1) };
    if (line.startsWith('-')) return { type: 'del', text: line.slice(1) };
    return { type: 'context', text: line.startsWith(' ') ? line.slice(1) : line };
  });
}

const STATUS_LABEL: Record<GitFileStatusCode, string> = {
  A: 'Added',
  M: 'Modified',
  D: 'Deleted',
  R: 'Renamed',
  C: 'Copied',
};

/** English key for `t()` — the screen translates it, this just names it. */
export function fileStatusLabel(status: GitFileStatusCode): string {
  return STATUS_LABEL[status] ?? status;
}

/** Local + remote branches filtered by the popover's search box. */
export function filterBranches<T extends { name: string }>(branches: T[], search: string): T[] {
  const q = search.trim().toLowerCase();
  if (!q) return branches;
  return branches.filter((b) => b.name.toLowerCase().includes(q));
}

/**
 * The backend's create-repo name rule (`src/git_panel.py`, 400 otherwise):
 * `[A-Za-z0-9._-]` only, non-empty. Checked client-side too so the "New
 * repository" dialog can disable Create instead of round-tripping a 400.
 */
export function isValidRepoName(name: string): boolean {
  return /^[A-Za-z0-9._-]+$/.test(name);
}

const HTTPS_REMOTE_RE = /^https:\/\/[^/]+\/([^/]+\/[^/]+?)(?:\.git)?\/?$/;
const SSH_REMOTE_RE = /^(?:ssh:\/\/)?git@[^:/]+:([^/]+\/[^/]+?)(?:\.git)?\/?$/;

/**
 * The same rewrite `PUT /repos/{id}/identity` performs server-side
 * (CONTRATO_GIT_2.md: "si era https `https://github.com/o/r.git` ->
 * `git@<alias>:o/r.git`"): pull the `owner/repo` path out of an https or
 * `git@host:owner/repo` remote and put it back together on the given ssh
 * host alias. `null` when `url` is neither shape — shown as "can't preview
 * this remote" instead of a wrong guess.
 */
export function rewriteRemoteToAlias(url: string, alias: string): string | null {
  const trimmed = url.trim();
  const path = HTTPS_REMOTE_RE.exec(trimmed)?.[1] ?? SSH_REMOTE_RE.exec(trimmed)?.[1];
  if (!path || !alias) return null;
  return `git@${alias}:${path}.git`;
}

/**
 * "Push those commits" only ever means something once commits are being
 * made at all (CONTRATO_GIT_2.md: "push deshabilitado si commit está off;
 * commit no exige rama") — the checkbox is disabled, not merely unchecked,
 * whenever `commit` is off.
 */
export function pushToggleDisabled(commit: boolean): boolean {
  return !commit;
}

/** `t()` key + values for the repo-header identity chip — the screen calls
 *  `t(result.key, result.values)` so this stays a plain function the check
 *  can call without a translation table. */
export function identityChipLabel(
  identity: { label: string; github_login: string | null } | null | undefined,
): { key: string; values?: Record<string, string> } {
  if (!identity) return { key: 'No SSH identity — using https/none' };
  return identity.github_login
    ? { key: 'SSH: {label} (login: {login})', values: { label: identity.label, login: identity.github_login } }
    : { key: 'SSH: {label}', values: { label: identity.label } };
}
