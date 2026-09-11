"""git_github.py -- OBJ-4 (Lote 84): create a GitHub remote via the `gh` CLI.

Luis has `gh` installed and authenticated to two accounts (`gh auth status`
-> Luissalet active, Mlgpigeon not, both ssh). "Create a repo and push
something" needs the GitHub-side repo to exist before `git push` can target
it; this module is the thin, testable wrapper around the three `gh`
subcommands that do that: `gh auth status` (who is logged in), `gh auth
token --user <login>` (a token for one of them, used ONLY as `GH_TOKEN` in a
child's environment -- never gh's own globally-active account, and never
logged or returned in any response body), and `gh repo create` (create the
GitHub-side repo, nothing local -- no `--source`/`--push`/`--remote`, so it
never touches the caller's working tree; `src/git_panel.add_remote` and
`push` do that afterwards, the same way a human would type it by hand).

Design notes
------------
* **`_run_gh` is the one seam that shells out** (never a shell, always a
  real argv, `stdin=DEVNULL` so a non-interactive `gh` never hangs waiting
  on a prompt) -- every other function in this module calls `_run_gh(...)`
  by name, never `subprocess.run` directly, so a test can monkeypatch
  `git_github._run_gh` and drive `gh_accounts`/`create_github_repo`/
  `gh_token` without a real `gh` binary anywhere (same pattern as
  `git_identities._probe_subprocess`). `gh_available()` (a bare
  `shutil.which` PATH check) is a separate, non-subprocess convenience --
  nothing here gates on it before calling `_run_gh`, precisely so a mocked
  `_run_gh` is sufficient on its own.
* **`gh repo create` output is not parsed for the URLs.** Different `gh`
  versions format its stdout differently; instead, once creation succeeds,
  `gh repo view --json nameWithOwner,url,sshUrl` asks for exactly the three
  fields needed, in a shape that doesn't drift across versions.
* **`gh auth status`'s accounts are cached 15s** (`_ACCOUNTS_CACHE_TTL`):
  `git_identities.list_identities` merges them into `GET /api/git/identities`
  and `routes/git_routes.py` re-checks an account's protocol when picking a
  remote URL, so without a short cache a repo list or an identity list would
  shell out to `gh` on every call.

Stdlib only, save for `src.native_env.native_host_environment` (the same
"environment for a child that is not ours" helper `git_panel`/
`git_identities` already use).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

GH_TIMEOUT_DEFAULT = 20.0
GH_TIMEOUT_CREATE = 30.0

_ACCOUNTS_CACHE_TTL = 15.0
_ACCOUNTS_LOCK = threading.Lock()
_ACCOUNTS_CACHE: Optional[Tuple[float, Dict[str, Any]]] = None

GH_MISSING_DETAIL = "GitHub CLI (gh) is not installed or not on PATH"


class GhNotAvailableError(Exception):
    """`gh` is not on PATH (or the binary vanished between the check and the
    call). Windows: `gh` may resolve as `gh.exe` -- `shutil.which("gh")`
    already finds that via `PATHEXT`, no special-casing needed."""


class GitHubRepoExistsError(Exception):
    """`gh repo create` refused: a repo with this name already exists under
    the target account (its stderr contains "already exists")."""

    def __init__(self, full_name: str):
        self.full_name = full_name
        super().__init__(f"{full_name} already exists on GitHub")


class GitHubCommandError(Exception):
    """A `gh` command exited non-zero for any other reason."""

    def __init__(self, cmd: Sequence[str], returncode: Optional[int], stdout: str, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        super().__init__(f"gh {' '.join(str(a) for a in self.cmd[:2])} failed rc={returncode}")


# ---------------------------------------------------------------------------
# Low-level `gh` execution
# ---------------------------------------------------------------------------
def gh_available() -> bool:
    """Bare PATH check -- a convenience for callers that just want a bool
    without spawning `gh`. Nothing in this module gates on it before calling
    `_run_gh`; see the module docstring."""
    return shutil.which("gh") is not None


def _gh_env(token: Optional[str] = None) -> Dict[str, str]:
    """`native_host_environment()` (strips our own venv markers, same as
    `git_panel`/`git_identities`) plus, when given, `GH_TOKEN` -- which makes
    `gh` act as that token's account for this ONE child process without
    touching `gh`'s own globally-active account (`gh auth switch`) at all.
    A stray `GH_TOKEN`/`GITHUB_TOKEN` inherited from the parent process is
    stripped when no token is given, so "use gh's own active account" can
    never be silently overridden by the environment."""
    env = native_host_environment()
    if token:
        env["GH_TOKEN"] = token
    else:
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
    return env


def _run_gh(args: Sequence[str], *, env: Optional[Dict[str, str]] = None,
           timeout: float = GH_TIMEOUT_DEFAULT) -> subprocess.CompletedProcess:
    """The one seam that shells out to `gh`. Its own function -- never a
    shell, always a real argv -- so tests can inject a double instead of
    mocking `subprocess.run` globally (same pattern as
    `git_identities._probe_subprocess`)."""
    exe = shutil.which("gh")
    if not exe:
        raise GhNotAvailableError()
    argv = [exe, *args]
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env if env is not None else _gh_env(), stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise GhNotAvailableError()
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=argv, returncode=124, stdout="",
            stderr=f"gh {args[0] if args else ''} timed out after {timeout:.0f}s",
        )


def gh_version() -> Optional[str]:
    try:
        proc = _run_gh(["--version"], timeout=5.0)
    except GhNotAvailableError:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", out)
    return m.group(1) if m else (out or None)


# ---------------------------------------------------------------------------
# `gh auth status` -> accounts
# ---------------------------------------------------------------------------
_LOGIN_LINE_RE = re.compile(r"[Ll]ogged in to \S+ account (\S+)")
_ACTIVE_RE = re.compile(r"Active account:\s*(true|false)", re.IGNORECASE)
_PROTOCOL_RE = re.compile(r"protocol:\s*(\S+)", re.IGNORECASE)
_SCOPES_RE = re.compile(r"[Tt]oken scopes:\s*(.*)")


def _parse_gh_auth_status(text: str) -> List[Dict[str, Any]]:
    """`gh auth status` prints one block per logged-in account (historically
    to stderr; newer versions use stdout -- `gh_accounts` feeds this BOTH
    streams combined, so either way works):

        github.com
          - Logged in to github.com account Luissalet (keyring)
          - Active account: true
          - Git operations protocol: ssh
          - Token: gho_****
          - Token scopes: 'gist', 'read:org', 'repo', 'workflow'

    Never the token itself -- this only ever reads the redacted `Token:`
    line's neighbours, and doesn't even capture that line."""
    accounts: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        m = _LOGIN_LINE_RE.search(line)
        if m:
            if current:
                accounts.append(current)
            current = {"login": m.group(1), "active": False, "protocol": None, "scopes": []}
            continue
        if current is None:
            continue
        m = _ACTIVE_RE.search(line)
        if m:
            current["active"] = m.group(1).lower() == "true"
            continue
        m = _PROTOCOL_RE.search(line)
        if m:
            current["protocol"] = m.group(1).strip()
            continue
        m = _SCOPES_RE.search(line)
        if m:
            current["scopes"] = [s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip().strip("'\"")]
            continue
    if current:
        accounts.append(current)
    return accounts


def invalidate_accounts_cache() -> None:
    global _ACCOUNTS_CACHE
    with _ACCOUNTS_LOCK:
        _ACCOUNTS_CACHE = None


def gh_accounts(*, use_cache: bool = True) -> Dict[str, Any]:
    """``GET /api/git/github/accounts`` payload:
    ``{"available", "version", "accounts": [{"login","active","protocol","scopes"}]}``.
    Never the token. `gh` missing (or `auth status` erroring outright, e.g.
    nobody logged in) reads as ``available`` reflecting whether `gh` itself
    was found, with an empty `accounts` list -- never an exception, since
    every caller (the accounts route, the identities merge, the create/
    publish routes) needs a value to render around, not a 500."""
    global _ACCOUNTS_CACHE
    if use_cache:
        with _ACCOUNTS_LOCK:
            hit = _ACCOUNTS_CACHE
        if hit is not None and (time.monotonic() - hit[0]) < _ACCOUNTS_CACHE_TTL:
            return hit[1]

    try:
        version = gh_version()
        proc = _run_gh(["auth", "status"], timeout=GH_TIMEOUT_DEFAULT)
    except GhNotAvailableError:
        result: Dict[str, Any] = {"available": False, "version": None, "accounts": []}
    else:
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        result = {"available": True, "version": version, "accounts": _parse_gh_auth_status(text)}

    if use_cache:
        with _ACCOUNTS_LOCK:
            _ACCOUNTS_CACHE = (time.monotonic(), result)
    return result


def gh_token(login: str) -> Optional[str]:
    """`gh auth token --user <login>` -- kept in memory only by the caller
    (`create_github_repo`, as `GH_TOKEN` in one child's environment); never
    logged, never put in a response body. `None` on any failure (missing
    `gh`, unknown login, ...) -- the caller's own `gh repo create` call will
    surface the real error."""
    if not login:
        return None
    try:
        proc = _run_gh(["auth", "token", "--user", login], timeout=GH_TIMEOUT_DEFAULT)
    except GhNotAvailableError:
        return None
    if proc.returncode != 0:
        return None
    token = (proc.stdout or "").strip()
    return token or None


# ---------------------------------------------------------------------------
# `gh repo create`
# ---------------------------------------------------------------------------
_ALREADY_EXISTS_RE = re.compile(r"already exists", re.IGNORECASE)


def create_github_repo(login: str, name: str, *, private: bool = True,
                       description: str = "") -> Dict[str, Any]:
    """`gh repo create <login>/<name> --private|--public [--description ...]`,
    run as `login` via `GH_TOKEN` (never switching `gh`'s own active
    account) -- creates the GitHub-side repo only, no local git action
    (`git_panel.add_remote`/`push` do that once the caller has the URL).
    Returns ``{"full_name", "html_url", "ssh_url", "https_url"}``.

    Raises `GhNotAvailableError` (`gh` missing), `GitHubRepoExistsError`
    (409 at the route layer -- the name is already taken under `login`), or
    `GitHubCommandError` (502 -- anything else `gh` refused with)."""
    full_name = f"{login}/{name}"
    token = gh_token(login)
    env = _gh_env(token)

    args = ["repo", "create", full_name, "--private" if private else "--public"]
    if description:
        args += ["--description", description]
    proc = _run_gh(args, env=env, timeout=GH_TIMEOUT_CREATE)
    if proc.returncode != 0:
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if _ALREADY_EXISTS_RE.search(combined):
            raise GitHubRepoExistsError(full_name)
        raise GitHubCommandError(args, proc.returncode, proc.stdout, proc.stderr)

    data: Dict[str, Any] = {}
    try:
        view_proc = _run_gh(["repo", "view", full_name, "--json", "nameWithOwner,url,sshUrl"],
                            env=env, timeout=GH_TIMEOUT_DEFAULT)
    except GhNotAvailableError:
        view_proc = None
    if view_proc is not None and view_proc.returncode == 0:
        try:
            data = json.loads(view_proc.stdout or "{}")
        except ValueError:
            data = {}

    html_url = data.get("url") or f"https://github.com/{full_name}"
    https_url = html_url if html_url.endswith(".git") else html_url.rstrip("/") + ".git"
    ssh_url = data.get("sshUrl") or f"git@github.com:{full_name}.git"
    resolved_full_name = data.get("nameWithOwner") or full_name
    return {"full_name": resolved_full_name, "html_url": html_url, "ssh_url": ssh_url, "https_url": https_url}
