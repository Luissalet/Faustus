"""github_pr.py -- "give the agent a GitHub issue and it ends with a pull
request." Two halves:

* Parsing/formatting helpers with no network or process calls at all
  (`parse_issue_ref`, `issue_brief`, `suggest_branch_name`,
  `pr_body_from_turn`, `detect_default_branch`) -- deterministic, unit-
  tested without mocking anything but `subprocess.run` for the two that read
  local git state (`parse_issue_ref` with a bare `#N`, `detect_default_branch`).

* The two calls that reach GitHub (`fetch_issue`, `open_pull_request`): the
  public REST API via `httpx`, with the token from
  `src.reach.credentials.get_token("github")` when one is configured, and a
  fallback to the `gh` CLI when no token is configured and `gh` is on PATH.
  Neither ever pushes a branch -- `open_pull_request` only opens the PR for a
  `head` the caller already pushed.

Used by the `github_issue` / `git_open_pr` agent tools in
`src/agent_tools/git_tools.py`; nothing here is agent- or tool-specific, so
it can be exercised directly in tests without going through the tool layer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
_REQUEST_TIMEOUT = 15.0

_ISSUE_BODY_CLIP = 2000
_MAX_COMMENTS = 20


class GithubPRError(Exception):
    """Raised for anything that stops `fetch_issue`/`open_pull_request` --
    a GitHub API error, a network failure, or the absence of both a token
    and the `gh` CLI. The message is meant to be shown to the model/user
    as-is."""


# ---------------------------------------------------------------------------
# parse_issue_ref
# ---------------------------------------------------------------------------
_URL_RE = re.compile(
    r"github\.com[:/](?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/(?:issues|pull)/(?P<number>\d+)"
)
_SHORT_RE = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)$")
_BARE_RE = re.compile(r"^#?(?P<number>\d+)$")
_REMOTE_PATH_RE = re.compile(r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$")


def _parse_remote_url(url: str) -> Optional[tuple]:
    """`owner, repo` out of an `origin` remote URL -- https, a plain
    `host:owner/repo.git` scp-like form, or an `ssh://` URL. Works for a
    custom SSH `Host` alias (e.g. `git@myhost:owner/repo.git`) since it
    never checks the hostname, only the trailing `owner/repo[.git]` path."""
    raw = (url or "").strip().replace("\\", "/")
    if not raw:
        return None
    if re.match(r"^[A-Za-z]:/", raw):
        # Windows drive path (a local bare repo as `origin`): plain path.
        path = raw
    elif "://" in raw:
        path = urlparse(raw).path
    elif "@" in raw and ":" in raw and raw.index(":") > raw.index("@"):
        # scp-like syntax: user@host:owner/repo.git (no scheme)
        _, _, path = raw.partition(":")
    elif ":" in raw and "/" not in raw.split(":", 1)[0]:
        _, _, path = raw.partition(":")
    else:
        path = raw
    m = _REMOTE_PATH_RE.search(path.strip("/"))
    if not m:
        return None
    return (m["owner"], m["repo"])


def _origin_owner_repo(workspace: Optional[str]) -> Optional[tuple]:
    if not workspace:
        # No workspace given -- never falls back to the process's own
        # current directory (which would resolve against whatever repo the
        # server process happens to be running from, not the caller's).
        return None
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_remote_url((proc.stdout or "").strip())


def parse_issue_ref(text: str, workspace: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """`{"owner", "repo", "number"}` from a full GitHub issue/PR URL,
    `owner/repo#12`, or a bare `#12`/`12` resolved against the workspace's
    `origin` remote. Returns None when it cannot be parsed, or a bare
    number is given with no `origin` remote to resolve it against."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = _URL_RE.search(raw)
    if m:
        return {"owner": m["owner"], "repo": m["repo"], "number": int(m["number"])}
    m = _SHORT_RE.match(raw)
    if m:
        return {"owner": m["owner"], "repo": m["repo"], "number": int(m["number"])}
    m = _BARE_RE.match(raw)
    if m:
        origin = _origin_owner_repo(workspace)
        if not origin:
            return None
        owner, repo = origin
        return {"owner": owner, "repo": repo, "number": int(m["number"])}
    return None


# ---------------------------------------------------------------------------
# fetch_issue
# ---------------------------------------------------------------------------
def _issue_headers(token: str) -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _normalize_labels(raw_labels: Any) -> List[str]:
    out = []
    for label in raw_labels or []:
        if isinstance(label, dict):
            out.append(str(label.get("name") or ""))
        else:
            out.append(str(label))
    return [l for l in out if l]


async def _fetch_issue_api(owner: str, repo: str, number: int, token: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, headers=_issue_headers(token)) as client:
        resp = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}")
        if resp.status_code >= 400:
            raise GithubPRError(
                f"GitHub API error {resp.status_code} fetching {owner}/{repo}#{number}: {resp.text[:200]}"
            )
        data = resp.json()
        comments: List[Dict[str, str]] = []
        if data.get("comments"):
            c_resp = await client.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments",
                params={"per_page": _MAX_COMMENTS},
            )
            if c_resp.status_code == 200:
                for c in c_resp.json()[:_MAX_COMMENTS]:
                    comments.append({
                        "author": (c.get("user") or {}).get("login", ""),
                        "body": c.get("body") or "",
                        "created_at": c.get("created_at", ""),
                    })
    return {
        "title": data.get("title") or "",
        "body": data.get("body") or "",
        "labels": _normalize_labels(data.get("labels")),
        "state": data.get("state") or "",
        "comments": comments,
        "url": data.get("html_url") or f"https://github.com/{owner}/{repo}/issues/{number}",
        "number": number,
        "owner": owner,
        "repo": repo,
    }


async def _fetch_issue_gh(owner: str, repo: str, number: int) -> Dict[str, Any]:
    path = shutil.which("gh")
    if not path:
        raise GithubPRError(
            f"no GitHub token configured and the `gh` CLI is not installed; "
            f"cannot fetch {owner}/{repo}#{number}"
        )
    cmd = [path, "issue", "view", str(number), "--repo", f"{owner}/{repo}",
           "--json", "title,body,labels,state,comments,url"]
    proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    if proc.returncode != 0:
        raise GithubPRError(f"gh issue view failed: {(proc.stderr or proc.stdout or '').strip()[:300]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise GithubPRError(f"gh issue view returned invalid JSON: {exc}") from exc
    comments = [
        {
            "author": (c.get("author") or {}).get("login", ""),
            "body": c.get("body") or "",
            "created_at": c.get("createdAt", ""),
        }
        for c in (data.get("comments") or [])[:_MAX_COMMENTS]
    ]
    return {
        "title": data.get("title") or "",
        "body": data.get("body") or "",
        "labels": _normalize_labels(data.get("labels")),
        "state": (data.get("state") or "").lower(),
        "comments": comments,
        "url": data.get("url") or f"https://github.com/{owner}/{repo}/issues/{number}",
        "number": number,
        "owner": owner,
        "repo": repo,
    }


async def fetch_issue(owner: str, repo: str, number: int, *, token: Optional[str] = None) -> Dict[str, Any]:
    """`{title, body, labels, state, comments[<=20], url}` for issue/PR
    `#number` in `owner/repo`. Tries the public REST API first (with `token`
    -- explicit, or `src.reach.credentials.get_token("github")` when not
    given); if that fails and no token was used, falls back to `gh issue
    view`; raises `GithubPRError` when neither works."""
    if token is None:
        from src.reach import credentials
        token = credentials.get_token("github")
    try:
        return await _fetch_issue_api(owner, repo, number, token)
    except (httpx.HTTPError, GithubPRError):
        if token:
            raise
        logger.debug("github_pr.fetch_issue: API attempt failed, trying gh CLI", exc_info=True)
        return await _fetch_issue_gh(owner, repo, number)


# ---------------------------------------------------------------------------
# issue_brief
# ---------------------------------------------------------------------------
_CHECKLIST_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+)$", re.MULTILINE)


def issue_brief(issue: Dict[str, Any]) -> str:
    """Compact markdown brief for the agent: title, labels, state, the body
    trimmed to ~2000 chars, and any `- [ ]` checklist items pulled out as
    acceptance hints."""
    title = str(issue.get("title") or "(untitled)")
    body = str(issue.get("body") or "").strip()
    if len(body) > _ISSUE_BODY_CLIP:
        body = body[:_ISSUE_BODY_CLIP].rstrip() + "…"
    labels = _normalize_labels(issue.get("labels"))
    lines = [f"# {title}", ""]
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    if issue.get("state"):
        lines.append(f"State: {issue['state']}")
    lines.append("")
    lines.append(body or "(no description)")
    checklist = _CHECKLIST_RE.findall(str(issue.get("body") or ""))
    if checklist:
        lines.append("")
        lines.append("Acceptance hints:")
        for mark, item in checklist:
            done = mark.strip().lower() == "x"
            lines.append(f"- [{'x' if done else ' '}] {item.strip()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# suggest_branch_name
# ---------------------------------------------------------------------------
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_BRANCH_LEN = 60


def suggest_branch_name(issue: Dict[str, Any]) -> str:
    """`fix/123-short-slug` (or `feat/...` when a label reads like a feature
    request) from an issue's number/title/labels."""
    number = issue.get("number") or issue.get("num") or 0
    title = str(issue.get("title") or "issue")
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    words = [w for w in slug.split("-") if w][:6]
    slug = "-".join(words) or "issue"
    labels = [l.lower() for l in _normalize_labels(issue.get("labels"))]
    prefix = "feat" if any("feature" in l or "enhancement" in l for l in labels) else "fix"
    name = f"{prefix}/{number}-{slug}" if number else f"{prefix}/{slug}"
    return name[:_MAX_BRANCH_LEN].rstrip("-")


# ---------------------------------------------------------------------------
# pr_body_from_turn
# ---------------------------------------------------------------------------
def pr_body_from_turn(issue: Optional[Dict[str, Any]], summary: str,
                       files_changed: Optional[List[str]], tests_line: str = "") -> str:
    """Markdown PR body: `Closes #N` (when `issue` has a number), the turn's
    own summary, a `Files changed` list, and a one-line test note."""
    lines: List[str] = []
    number = (issue or {}).get("number")
    if number:
        lines.append(f"Closes #{number}")
        lines.append("")
    summary = (summary or "").strip()
    if summary:
        lines.append(summary)
        lines.append("")
    if files_changed:
        lines.append("## Files changed")
        for f in files_changed:
            lines.append(f"- {f}")
        lines.append("")
    tests_line = (tests_line or "").strip()
    if tests_line:
        lines.append("## Tests")
        lines.append(tests_line)
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# detect_default_branch -- local-only, no network
# ---------------------------------------------------------------------------
def detect_default_branch(repo_root: str) -> str:
    """The repo's default branch: `origin/HEAD`'s target when set, else
    whichever of `main`/`master` exists as a remote-tracking branch, else
    `main`. Never touches the network -- reads local refs only (a prior
    `git fetch`/clone already populated `refs/remotes/origin/*`)."""
    from src import git_panel

    proc = git_panel.run_git(repo_root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
    if proc.returncode == 0:
        ref = (proc.stdout or "").strip()
        if ref.startswith("origin/"):
            return ref[len("origin/"):]
    for candidate in ("main", "master"):
        check = git_panel.run_git(repo_root, "show-ref", "--verify", "--quiet",
                                   f"refs/remotes/origin/{candidate}")
        if check.returncode == 0:
            return candidate
    return "main"


# ---------------------------------------------------------------------------
# open_pull_request
# ---------------------------------------------------------------------------
async def _find_existing_pr(client: httpx.AsyncClient, owner: str, repo: str, head: str,
                             state: str = "open") -> Optional[Dict[str, Any]]:
    resp = await client.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
        params={"head": f"{owner}:{head}", "state": state},
    )
    if resp.status_code == 200:
        items = resp.json()
        if items:
            return items[0]
    return None


async def _open_pull_request_api(owner: str, repo: str, token: str, *, base: str, head: str,
                                  title: str, body: str, draft: bool) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, headers=_issue_headers(token)) as client:
        existing = await _find_existing_pr(client, owner, repo, head, state="open")
        if existing:
            return {"url": existing.get("html_url"), "number": existing.get("number"), "created": False}
        resp = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "base": base, "head": head, "draft": bool(draft)},
        )
        if resp.status_code == 201:
            pr = resp.json()
            return {"url": pr.get("html_url"), "number": pr.get("number"), "created": True}
        if resp.status_code == 422:
            existing = await _find_existing_pr(client, owner, repo, head, state="all")
            if existing:
                return {"url": existing.get("html_url"), "number": existing.get("number"), "created": False}
        raise GithubPRError(
            f"GitHub API error {resp.status_code} opening a pull request for {owner}/{repo}:{head}: "
            f"{resp.text[:300]}"
        )


async def _open_pull_request_gh(workspace: Optional[str], *, base: str, head: str,
                                 title: str, body: str, draft: bool) -> Dict[str, Any]:
    path = shutil.which("gh")
    if not path:
        raise GithubPRError("no GitHub token configured and the `gh` CLI is not installed; cannot open a pull request")
    cmd = [path, "pr", "create", "--base", base, "--head", head, "--title", title, "--body", body]
    if draft:
        cmd.append("--draft")
    proc = await asyncio.to_thread(subprocess.run, cmd, cwd=workspace or None,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "already exists" in err.lower():
            view = await asyncio.to_thread(
                subprocess.run, [path, "pr", "view", head, "--json", "url,number"],
                cwd=workspace or None, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            )
            if view.returncode == 0:
                try:
                    data = json.loads(view.stdout or "{}")
                except ValueError:
                    data = {}
                if data.get("url"):
                    return {"url": data.get("url"), "number": data.get("number"), "created": False}
        raise GithubPRError(f"gh pr create failed: {err[:300]}")
    out = (proc.stdout or "").strip()
    url = out.splitlines()[-1].strip() if out else ""
    m = re.search(r"/pull/(\d+)", url)
    number = int(m[1]) if m else None
    return {"url": url, "number": number, "created": True}


async def open_pull_request(workspace: str, *, base: str, head: str, title: str, body: str,
                             draft: bool = False, token: Optional[str] = None) -> Dict[str, Any]:
    """Open (or return the already-existing) pull request from `head` into
    `base` on the repo `workspace`'s `origin` remote points at. Never
    pushes -- `head` must already exist on the remote. `{url, number,
    created}`; `created` is False when a PR for `head` already existed."""
    owner_repo = _origin_owner_repo(workspace)
    if not owner_repo:
        raise GithubPRError(f"could not resolve an owner/repo from the 'origin' remote at {workspace!r}")
    owner, repo = owner_repo
    if token is None:
        from src.reach import credentials
        token = credentials.get_token("github")
    if token:
        return await _open_pull_request_api(owner, repo, token, base=base, head=head,
                                             title=title, body=body, draft=draft)
    return await _open_pull_request_gh(workspace, base=base, head=head, title=title, body=body, draft=draft)
