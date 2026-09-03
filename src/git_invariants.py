"""git_invariants.py — what must be true before anything commits for the user.

An automatic commit that fires against a repository in the wrong state is
destructive and silent: committing in the middle of a rebase turns a conflict
resolution into a lost one, a commit on a detached HEAD is unreachable the
moment the next checkout happens, and a commit in a repository the caller
believed was a different one lands somewhere nobody will look.

None of these are recoverable by the person watching the chat, because none of
them look like a failure. So they are checked first, and a checkpoint that
refuses is reported instead.

Stdlib only.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 15.0

# Files and directories git leaves behind while an operation is half-finished.
# REBASE_HEAD alone is not enough: an interactive rebase that stops on its very
# first pick has rebase-merge/ but no REBASE_HEAD yet.
_IN_PROGRESS = (
    ("MERGE_HEAD", "a merge"),
    ("CHERRY_PICK_HEAD", "a cherry-pick"),
    ("REVERT_HEAD", "a revert"),
    ("BISECT_LOG", "a bisect"),
    ("REBASE_HEAD", "a rebase"),
    ("rebase-merge", "a rebase"),
    ("rebase-apply", "a rebase or a patch series"),
)

_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<rest>.*)$", re.DOTALL)
# `[user@]host:path` — the host part carries no slash, which is what separates
# an ssh remote from a local relative path that happens to contain a colon.
_SCP_RE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^@/:]+):(?P<path>.*)$", re.DOTALL)


@dataclass
class Preconditions:
    """`ok` is the decision; `problems` are sentences to show the person."""
    ok: bool
    problems: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _git(repo_dir: str, args: Sequence[str], *, timeout: float = _GIT_TIMEOUT):
    """Run one git command in `repo_dir`. Never a shell, never a prompt.

    The child gets `native_host_environment` because a repository's hooks are
    the user's own code: run them with our virtualenv on PATH and a python
    pre-commit hook silently resolves against our interpreter.
    """
    try:
        return subprocess.run(
            ["git", *args], cwd=repo_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env=native_host_environment(extra={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}),
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("[git-invariants] git %s in %s failed: %s", list(args)[:2], repo_dir, e)
        return None


def _out(proc) -> str:
    if proc is None or proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _tidy_path(path: str) -> str:
    """Drop the trailing slash and the `.git` suffix that mean nothing."""
    path = path.strip().replace("\\", "/").strip("/")
    if path.endswith(".git"):
        path = path[:-4].rstrip("/")
    return path


def canonical_git_remote(repo_dir: str) -> Optional[str]:
    """`origin` as `host/owner/repo`, or None when there is no origin.

    Reduces the forms of the same remote to one string: `git@host:owner/repo.git`,
    `ssh://git@host:22/owner/repo`, and `https://host/owner/repo` all canonicalise
    to `host/owner/repo`. An ssh alias from the user's ssh config
    (`git@Luissalet:owner/repo.git`) keeps the alias as the host rather than
    failing to parse — an unparseable remote would silently compare equal to
    nothing and turn every expectation into a refusal.
    """
    raw = _out(_git(repo_dir, ["config", "--get", "remote.origin.url"]))
    if not raw:
        return None
    return _canonical_remote_url(raw)


def _canonical_remote_url(raw: str) -> Optional[str]:
    url = (raw or "").strip()
    if not url:
        return None

    scheme = _SCHEME_RE.match(url)
    if scheme:
        rest = scheme.group("rest")
        authority, _, path = rest.partition("/")
        authority = authority.rpartition("@")[2]        # drop any user[:password]@
        host = authority.split(":", 1)[0]               # drop any :port
        if not host:                                    # file:///srv/git/repo.git
            return _local_remote(path if path.startswith("/") else "/" + path)
        return f"{host.lower()}/{_tidy_path(path)}".rstrip("/")

    scp = _SCP_RE.match(url)
    if scp:
        host, path = scp.group("host"), scp.group("path")
        # `C:\repos\thing` is a drive letter, not a host.
        if not (len(host) == 1 and path[:1] in ("/", "\\")):
            return f"{host.lower()}/{_tidy_path(path)}".rstrip("/")

    return _local_remote(url)


def _local_remote(path: str) -> Optional[str]:
    cleaned = os.path.normpath(_tidy_path(path)) if path.strip() else ""
    if not cleaned or cleaned == ".":
        return None
    return os.path.normcase(cleaned)


def _same_remote(actual: Optional[str], expected: str) -> bool:
    """Compare canonical forms case-insensitively.

    Hosting services treat `Owner/Repo` and `owner/repo` as one repository, and
    refusing a legitimate commit over a capital letter is the worse mistake.
    """
    wanted = _canonical_remote_url(expected)
    if actual is None or wanted is None:
        return actual == wanted
    return actual.casefold() == wanted.casefold()


def check_preconditions(repo_dir: str, *, expect_remote: Optional[str] = None,
                        expect_branch: Optional[str] = None,
                        allow_detached: bool = False) -> Preconditions:
    """Is `repo_dir` in a state where committing for the user is safe?

    Every problem found is reported, not just the first, so the person sees the
    whole picture instead of fixing one thing and hitting the next.
    """
    problems: List[str] = []

    if not repo_dir or not os.path.isdir(repo_dir):
        return Preconditions(False, [f"{repo_dir or 'The workspace'} is not a directory that exists."])
    if not shutil.which("git"):
        return Preconditions(False, ["git is not installed or not on PATH, so nothing can be committed."])

    flags = _out(_git(repo_dir, ["rev-parse", "--is-inside-work-tree",
                                "--is-bare-repository", "--is-inside-git-dir"])).splitlines()
    if len(flags) < 3:
        return Preconditions(False, [f"{repo_dir} is not inside a git repository."])
    inside_work_tree, is_bare, inside_git_dir = (f.strip() == "true" for f in flags[:3])
    if is_bare:
        problems.append(f"{repo_dir} is a bare repository, which has no working tree to commit from.")
    elif inside_git_dir:
        problems.append(f"{repo_dir} is inside the .git directory, not the working tree.")
    elif not inside_work_tree:
        problems.append(f"{repo_dir} is not inside a git working tree.")
    if problems:
        return Preconditions(False, problems)      # nothing below can be trusted

    git_dir = _out(_git(repo_dir, ["rev-parse", "--absolute-git-dir"]))
    if git_dir:
        seen = set()
        for name, what in _IN_PROGRESS:
            if what in seen or not os.path.exists(os.path.join(git_dir, name)):
                continue
            seen.add(what)
            problems.append(
                f"{what.capitalize()} is in progress in {repo_dir}. Finish or abort it "
                f"(git rebase --continue / --abort, git merge --abort) before committing."
            )

    branch_proc = _git(repo_dir, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    branch = _out(branch_proc)
    detached = branch_proc is not None and branch_proc.returncode != 0
    if detached and not allow_detached:
        head = _out(_git(repo_dir, ["rev-parse", "--short", "HEAD"])) or "an unnamed commit"
        problems.append(
            f"HEAD is detached at {head} in {repo_dir}. A commit made here belongs to no "
            f"branch and is lost at the next checkout; check out a branch first."
        )

    if expect_remote is not None:
        actual = canonical_git_remote(repo_dir)
        if actual is None:
            problems.append(
                f"{repo_dir} has no 'origin' remote, but the checkpoint expected {expect_remote}."
            )
        elif not _same_remote(actual, expect_remote):
            problems.append(
                f"The 'origin' remote of {repo_dir} is {actual}, not the expected {expect_remote}. "
                f"This is not the repository the checkpoint was configured for."
            )

    if expect_branch is not None:
        if detached:
            problems.append(f"The checkpoint expected branch '{expect_branch}', but HEAD is detached.")
        elif branch != expect_branch:
            problems.append(
                f"{repo_dir} is on branch '{branch or 'an unknown branch'}', not the expected "
                f"'{expect_branch}'. Switch branch or update the expectation before committing."
            )

    return Preconditions(not problems, problems)
