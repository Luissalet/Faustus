"""agent_tools/git_tools.py — git tools for the agent (Lote 87, OBJ-4).

Luis's own framing (CONTRATO_GIT_4, Lote 87): "the user says 'commit this and
push' and the model does it through tools, with approval and respecting the
policy." Nine tools, all thin executors over `src.git_panel` (the same
argv-only, no-`shell=True`, hardened-flags git runner the Source Control
panel itself uses — see that module's docstring) — no subprocess of our own:

    git_status    read   branch, ahead/behind, staged/unstaged/untracked, last N commits
    git_log       read   commit history (limit, ref)
    git_diff      read   working tree / staged / one commit's diff, clipped to 60 KB
    git_branch    write  create (+ optionally checkout) a branch
    git_checkout  write  switch branches
    git_commit    write  stage explicit paths and commit with the repo's OWN identity
    git_push      remote push (never force — git_panel.push has no force flag at all)
    git_pull      remote pull --ff-only
    git_fetch     remote fetch

Workspace confinement
----------------------
Every tool resolves its (optional) `path` argument through
`src.tool_execution._resolve_tool_path` — the SAME allowlist read_file/
write_file/manage_spreadsheet already confine to (the turn's workspace, plus
any other file/folder roots linked to the session's project). A `path` that
escapes those roots, or a workspace with no repo at or above it, is refused
before any `git` process runs — `error_class` `git.outside_workspace` /
`git.not_a_repo`. An omitted `path` defaults to the active workspace itself.

Agent git policy gate (src/agent_git_policy.py)
-------------------------------------------------
`git_commit` (`policy.commit`), `git_branch`/`git_checkout` (`policy.
use_branch`) and `git_push` (`policy.push`) each check the EFFECTIVE policy
for the target repo (global setting, overridable per repo — the same
`effective_policy()` the panel's own `GET /api/git/repos/{id}/policy` and the
`before_turn`/`after_turn` turn hooks already use) before touching anything.
When the relevant field is `False`, the call is refused UNLESS the human
explicitly approved it — see `_policy_denied()` below for exactly what
"approved" means and how a tool detects it.  `git_pull`/`git_fetch` read from
the remote and only ever fast-forward local refs, so they carry no policy
gate — same risk class as the panel's own always-on fetch/pull buttons.

`git_diverged`/`git.dirty`/`git.rejected`/`git.no_identity`/`git.
nothing_to_commit` mirror the panel's own error vocabulary (`docs/api/git.
md`) byte for byte, so a client that already renders the panel's errors
renders the agent's without new code.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src import agent_git_policy
from src import git_panel

logger = logging.getLogger(__name__)

_DIFF_CLIP_BYTES = 60_000


# ---------------------------------------------------------------------------
# Argument parsing (same permissive JSON-object shape every action-dispatched
# tool in this codebase accepts — see spreadsheet_tools.py / code_tools.py)
# ---------------------------------------------------------------------------
def _args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _owner(ctx: dict) -> str:
    return str((ctx or {}).get("owner") or "")


# ---------------------------------------------------------------------------
# Workspace confinement -> repo resolution
# ---------------------------------------------------------------------------
class _OutsideWorkspace(ValueError):
    """`path` (or the active workspace, when omitted) escapes the turn's
    workspace / linked project folders."""


class _NotARepo(ValueError):
    """No `.git` found at or above the confined path."""


def _confined_path(raw_path: str) -> str:
    """`raw_path` resolved and confined to the turn's active workspace roots
    (`src.tool_execution._resolve_tool_path` — the same allowlist read_file/
    write_file use), or the active workspace itself when `raw_path` is empty.
    """
    from src.tool_execution import _resolve_tool_path, get_active_workspace

    raw = str(raw_path or "").strip()
    if not raw:
        ws = get_active_workspace()
        if not ws:
            raise _OutsideWorkspace(
                "no active workspace is bound to this turn -- pass `path`, "
                "or run this inside a chat with a workspace/project set"
            )
        return ws
    try:
        return _resolve_tool_path(raw)
    except ValueError as exc:
        raise _OutsideWorkspace(str(exc)) from exc


def _repo_root(raw_path: str) -> str:
    """The git repo containing `raw_path` (or the active workspace) --
    `path` may be a subdirectory of the repo, not its root.  Raises
    `_OutsideWorkspace` / `_NotARepo`; callers turn those into `error_class`
    `git.outside_workspace` / `git.not_a_repo`."""
    confined = _confined_path(raw_path)
    root = git_panel.repo_toplevel(confined)
    if not root:
        raise _NotARepo(f"no git repository found at or above {confined!r}")
    return root


def _repo_error(tool: str, exc: Exception) -> Dict[str, Any]:
    error_class = "git.outside_workspace" if isinstance(exc, _OutsideWorkspace) else "git.not_a_repo"
    return {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": error_class}


def _git_error(tool: str, exc: Exception) -> Dict[str, Any]:
    """Map a `src.git_panel` exception onto the same `error_class` vocabulary
    `docs/api/git.md` already documents for the panel's own routes."""
    if isinstance(exc, git_panel.GitNotFoundError):
        return {"error": f"{tool}: git is not installed on this host", "exit_code": 1,
                "error_class": "dependency.missing"}
    if isinstance(exc, git_panel.GitDirtyCheckoutError):
        return {"error": f"{tool}: would overwrite local changes", "exit_code": 1,
                "error_class": "git.dirty", "dirty": exc.paths}
    if isinstance(exc, git_panel.GitDivergedError):
        return {"error": f"{tool}: local and upstream diverged (ahead={exc.ahead}, behind={exc.behind})",
                "exit_code": 1, "error_class": "git.diverged", "ahead": exc.ahead, "behind": exc.behind}
    if isinstance(exc, git_panel.GitRejectedError):
        return {"error": f"{tool}: rejected by the remote", "exit_code": 1,
                "error_class": "git.rejected", "stderr": git_panel.stderr_snippet(exc.stderr)}
    if isinstance(exc, git_panel.GitNoIdentityError):
        return {"error": f"{tool}: the repo has no user.name/user.email configured", "exit_code": 1,
                "error_class": "git.no_identity"}
    if isinstance(exc, git_panel.GitNothingToCommitError):
        return {"error": f"{tool}: nothing to commit (empty message, or nothing staged)", "exit_code": 1,
                "error_class": "git.nothing_to_commit"}
    if isinstance(exc, git_panel.GitCommandError):
        return {"error": f"{tool}: {git_panel.stderr_snippet(exc.stderr or exc.stdout)}", "exit_code": 1,
                "error_class": "git.command_failed"}
    return {"error": f"{tool}: {exc}", "exit_code": 1, "error_class": "git.command_failed"}


# ---------------------------------------------------------------------------
# Agent git policy gate (src/agent_git_policy.py)
# ---------------------------------------------------------------------------
def _effective_policy(owner: str, repo_root: str) -> Dict[str, Any]:
    repo_id = git_panel.compute_repo_id(repo_root)
    return agent_git_policy.effective_policy(owner, repo_id)["effective"]


def _human_approved(ctx: dict, args: Dict[str, Any]) -> bool:
    """Whether a human explicitly authorized THIS exact tool call, checked
    two ways:

    1. `ctx["human_approved"]` -- set by `src.tool_execution.execute_tool_block`
       to whether this call's content matched (byte for byte, via
       `ExactToolApproval.claim`) a sealed `src.tool_approvals.
       PendingToolApproval` the user answered on an approval card. This is
       the SAME mechanism every other approval-gated tool in this codebase
       (desktop input, a destructive-command-guard verdict, a post-
       external-context write) already relies on -- no new approval
       subsystem, per BRIEF_CIERRE's rule 4. It fires whenever this run's
       security context already required a card for some other reason (an
       earlier fetched page, a guard-flagged shell command, ...) and the
       user approved it for this exact git call too.

    2. `args["user_confirmed"]` -- the model's own record that it asked the
       user (via `ask_user`) whether to override the repo's policy and the
       user said yes, then retried this exact call with the flag set. This
       is the same "ask, then retry with an explicit flag" shape
       `install_dependencies` already uses (`src/agent_tools/exec_tools.py`)
       for a plan that needs a human's yes with no sealed card in play --
       there is no card for an ordinary, untainted turn ("commit this and
       push"), since no gate would otherwise have blocked it to create one.

    A model running unattended (a scheduled task, no human turn to relay an
    answer from) has neither: `ctx["human_approved"]` is false because no
    card was ever sealed, and nothing tells it to set `user_confirmed`. That
    is deliberate -- "en modo autónomo sin aprobación, no" (CONTRATO_GIT_4).
    """
    return bool((ctx or {}).get("human_approved")) or bool(args.get("user_confirmed"))


def _policy_denied(tool: str, field: str, ctx: dict, args: Dict[str, Any], *, allowed: bool) -> Optional[Dict[str, Any]]:
    """`None` when the action may proceed, else the refusal result."""
    if allowed or _human_approved(ctx, args):
        return None
    return {
        "error": (
            f"{tool}: refused -- this repo's git agent policy has {field}=false. "
            "Ask the user for explicit approval and retry this EXACT call with "
            "\"user_confirmed\": true once they say yes, or the user can allow it "
            "from Settings > Git policy (global or per-repo)."
        ),
        "exit_code": 1,
        "policy": "git_agent_policy",
        "git_policy_field": field,
    }


# ---------------------------------------------------------------------------
# Diff clipping (AGENT tool cap: 60 KB -- distinct from the panel's own
# 200 KB `git_panel.MAX_DIFF_BYTES`, since this text also has to fit a model's
# context window, not just a browser tab)
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int = _DIFF_CLIP_BYTES) -> tuple[str, bool]:
    data = (text or "").encode("utf-8", "replace")
    if len(data) <= limit:
        return text or "", False
    return data[:limit].decode("utf-8", "replace"), True


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------
class GitStatusTool:
    """`git_status`: branch, ahead/behind, staged/unstaged/untracked, last N commits."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_status", exc)
        try:
            limit = max(1, min(int(args.get("limit") or 10), 100))
        except (TypeError, ValueError):
            limit = 10
        status = git_panel.repo_status(repo_root)
        log = git_panel.log_commits(repo_root, limit=limit)
        lines = [
            f"branch: {status['branch'] or ('(detached)' if status['detached'] else '(unborn)')}",
            f"ahead={status['ahead']} behind={status['behind']} upstream={status['upstream'] or '(none)'}",
            f"staged={len(status['staged'])} unstaged={len(status['unstaged'])} "
            f"untracked={len(status['untracked'])} conflicts={len(status['conflicts'])}",
        ]
        for c in log["commits"][:limit]:
            lines.append(f"{c['short']} {c['message']}")
        return {
            "output": "\n".join(lines), "exit_code": 0, "repo_root": repo_root,
            "branch": status["branch"], "detached": status["detached"],
            "ahead": status["ahead"], "behind": status["behind"], "upstream": status["upstream"],
            "staged": status["staged"], "unstaged": status["unstaged"],
            "untracked": status["untracked"], "conflicts": status["conflicts"],
            "commits": log["commits"],
        }


class GitLogTool:
    """`git_log`: commit history (`limit`, `ref`)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_log", exc)
        try:
            limit = max(1, min(int(args.get("limit") or 20), 500))
        except (TypeError, ValueError):
            limit = 20
        result = git_panel.log_commits(
            repo_root, limit=limit, cursor=str(args.get("cursor") or "") or None,
            ref=str(args.get("ref") or ""),
        )
        lines = [f"{c['short']} {c['date']} {c['author']}: {c['message']}" for c in result["commits"]]
        return {
            "output": "\n".join(lines) or "(no commits)", "exit_code": 0, "repo_root": repo_root,
            "commits": result["commits"], "next_cursor": result["next_cursor"],
        }


class GitDiffTool:
    """`git_diff`: working tree / staged / one commit's diff, clipped to 60 KB."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_diff", exc)
        commit = str(args.get("commit") or "").strip()
        raw_path = str(args.get("path_in_repo") or args.get("file") or "").strip()
        staged = bool(args.get("staged"))

        rel_path: Optional[str] = None
        if raw_path:
            rel_path = git_panel.safe_rel_path(repo_root, raw_path)
            if rel_path is None:
                return {"error": f"git_diff: path {raw_path!r} escapes the repository", "exit_code": 1}

        if commit and rel_path:
            result = git_panel.commit_file_diff(repo_root, commit, rel_path)
            text, binary = result["diff"], result["binary"]
        elif commit:
            proc = git_panel.run_git(
                repo_root, "diff-tree", "-p", "--root", "--no-color", "--no-ext-diff", "-M", "-C", commit, "--",
            )
            if proc.returncode != 0:
                return {"error": f"git_diff: {git_panel.stderr_snippet(proc.stderr)}", "exit_code": 1,
                        "error_class": "git.command_failed"}
            text = proc.stdout
            binary = "Binary files" in text
        elif rel_path:
            result = git_panel.working_diff(repo_root, rel_path, staged=staged)
            text, binary = result["diff"], result["binary"]
        else:
            diff_args = ["diff", "--no-color", "--no-ext-diff", "-M", "-C"]
            if staged:
                diff_args.append("--cached")
            proc = git_panel.run_git(repo_root, *diff_args)
            text = proc.stdout if proc.returncode in (0, 1) else ""
            binary = "Binary files" in text

        clipped, truncated = _clip(text)
        return {
            "output": clipped or "(no differences)", "exit_code": 0, "repo_root": repo_root,
            "diff": clipped, "truncated": truncated, "binary": binary,
            "path": rel_path, "commit": commit or None, "staged": staged,
        }


# ---------------------------------------------------------------------------
# Write tools (agent git policy gated)
# ---------------------------------------------------------------------------
class GitBranchTool:
    """`git_branch`: create (and by default check out) a branch.  Gated by
    `policy.use_branch`."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_branch", exc)
        policy = _effective_policy(_owner(ctx), repo_root)
        denial = _policy_denied("git_branch", "use_branch", ctx, args, allowed=bool(policy.get("use_branch")))
        if denial:
            return denial
        name = str(args.get("name") or "").strip()
        if not name:
            return {"error": "git_branch: `name` is required", "exit_code": 1}
        start_point = str(args.get("start_point") or "").strip() or None
        checkout = True if args.get("checkout") is None else bool(args.get("checkout"))
        try:
            branch = git_panel.create_branch(repo_root, name, start_point=start_point, checkout=checkout)
        except (git_panel.GitDirtyCheckoutError, git_panel.GitCommandError, git_panel.GitNotFoundError) as exc:
            return _git_error("git_branch", exc)
        return {
            "output": f"Branch {branch!r} ready" + (" (checked out)" if checkout else " (not checked out)"),
            "exit_code": 0, "branch": branch, "repo_root": repo_root,
        }


class GitCheckoutTool:
    """`git_checkout`: switch to an existing branch.  Gated by `policy.use_branch`."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_checkout", exc)
        policy = _effective_policy(_owner(ctx), repo_root)
        denial = _policy_denied("git_checkout", "use_branch", ctx, args, allowed=bool(policy.get("use_branch")))
        if denial:
            return denial
        branch = str(args.get("branch") or "").strip()
        if not branch:
            return {"error": "git_checkout: `branch` is required", "exit_code": 1}
        try:
            result_branch = git_panel.checkout_branch(repo_root, branch)
        except (git_panel.GitDirtyCheckoutError, git_panel.GitCommandError, git_panel.GitNotFoundError) as exc:
            return _git_error("git_checkout", exc)
        return {"output": f"Checked out {result_branch!r}", "exit_code": 0,
                "branch": result_branch, "repo_root": repo_root}


class GitCommitTool:
    """`git_commit`: stage EXPLICIT paths (never `-A`) and commit with the
    repo's own configured identity.  Gated by `policy.commit`.

    `paths` is required: the tool has no reliable, tamper-proof record of
    which files THIS turn's harness touched (that ledger is an in-process
    object private to `src.agent_loop`'s own turn loop, not reachable from a
    tool call -- see the Lote 87 report's "Cambios necesarios en ficheros
    ajenos" for the one-line wiring that would change this). Falling back to
    "stage everything" when the list is missing would be exactly the
    implicit `-A` the contract forbids, so this refuses instead.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_commit", exc)
        policy = _effective_policy(_owner(ctx), repo_root)
        denial = _policy_denied("git_commit", "commit", ctx, args, allowed=bool(policy.get("commit")))
        if denial:
            return denial
        message = str(args.get("message") or "").strip()
        if not message:
            return {"error": "git_commit: `message` is required", "exit_code": 1}
        raw_paths = args.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            return {
                "error": "git_commit: `paths` (a non-empty list of files to stage) is required. "
                         "Never `git add -A` -- name exactly the files this change touched.",
                "exit_code": 1,
            }
        rel_paths: List[str] = []
        for p in raw_paths:
            rel = git_panel.safe_rel_path(repo_root, str(p))
            if rel is None:
                return {"error": f"git_commit: path {p!r} escapes the repository", "exit_code": 1}
            rel_paths.append(rel)
        try:
            git_panel.stage(repo_root, paths=rel_paths)
            result = git_panel.commit(repo_root, message, amend=bool(args.get("amend")))
        except (git_panel.GitNoIdentityError, git_panel.GitNothingToCommitError,
                git_panel.GitCommandError, git_panel.GitNotFoundError) as exc:
            return _git_error("git_commit", exc)
        return {
            "output": f"Committed {result['short']}: {result['message']}", "exit_code": 0,
            "sha": result["sha"], "short": result["short"], "repo_root": repo_root,
            "paths": rel_paths,
        }


# ---------------------------------------------------------------------------
# Remote tools
# ---------------------------------------------------------------------------
class GitPushTool:
    """`git_push`: never force (git_panel.push has no force flag at all).
    Gated by `policy.push`."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_push", exc)
        policy = _effective_policy(_owner(ctx), repo_root)
        denial = _policy_denied("git_push", "push", ctx, args, allowed=bool(policy.get("push")))
        if denial:
            return denial
        remote = str(args.get("remote") or "").strip() or None
        branch = str(args.get("branch") or "").strip() or None
        set_upstream = bool(args.get("set_upstream"))
        try:
            output = git_panel.push(repo_root, remote=remote, branch=branch, set_upstream=set_upstream)
        except (git_panel.GitRejectedError, git_panel.GitCommandError, git_panel.GitNotFoundError) as exc:
            return _git_error("git_push", exc)
        return {"output": output.strip() or "Pushed.", "exit_code": 0, "repo_root": repo_root}


class GitPullTool:
    """`git_pull`: `--ff-only` (never a merge/rebase). Not policy-gated --
    same risk class as the panel's own always-on Pull button."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_pull", exc)
        remote = str(args.get("remote") or "").strip() or None
        branch = str(args.get("branch") or "").strip() or None
        try:
            output = git_panel.pull(repo_root, remote=remote, branch=branch)
        except (git_panel.GitDivergedError, git_panel.GitCommandError, git_panel.GitNotFoundError) as exc:
            return _git_error("git_pull", exc)
        return {"output": output.strip() or "Already up to date.", "exit_code": 0, "repo_root": repo_root}


class GitFetchTool:
    """`git_fetch`. Not policy-gated -- read-only against the local
    working tree (only updates remote-tracking refs)."""

    async def execute(self, content: str, ctx: dict) -> dict:
        args = _args(content)
        try:
            repo_root = _repo_root(str(args.get("path") or ""))
        except (_OutsideWorkspace, _NotARepo) as exc:
            return _repo_error("git_fetch", exc)
        remote = str(args.get("remote") or "").strip() or None
        prune = bool(args.get("prune"))
        try:
            output = git_panel.fetch(repo_root, remote=remote, prune=prune)
        except (git_panel.GitCommandError, git_panel.GitNotFoundError) as exc:
            return _git_error("git_fetch", exc)
        return {"output": output.strip() or "Already up to date.", "exit_code": 0, "repo_root": repo_root}
