"""ci_failures.py -- what actually broke, from the CI run's own logs.

A red check on a pull request says "it failed"; it never says which file,
which test, or who touched that file last. This module answers that: it
finds the repo behind the current workspace, pulls the latest failed
GitHub Actions run (or one named by id), reads the failed jobs' raw logs,
and extracts the concrete failure blocks a person would grep for by hand --
a pytest `FAILED`, a jest `●`, a `tsc`/eslint diagnostic, a cargo
`error[Exxxx]`, a `go test` `--- FAIL`, an npm `ERR!`, a `##[error]`
annotation, or a generic `Error:`/`Traceback` block.

    repo_from_workspace(workspace)   -> (owner, repo) from `git remote`
    list_runs / list_jobs / job_log  -> thin GitHub REST wrappers, `gh` CLI
                                         fallback, clear error otherwise
    extract_failures(log_text)       -> List[FailureBlock] (deduped)
    map_to_workspace(blocks, ws)     -> attaches resolved path, last-touch
                                         author/date, and (best effort) the
                                         code-graph community for that file
    analyze(workspace, ...)          -> Analysis (run + jobs + mapped blocks)
    propose_fixes(analysis, owner)   -> the utility model's ranked guesses,
                                         or the mapping alone with no model

Read-only and network: nothing here ever writes to the repo or to GitHub.
A token is read through `src.reach.credentials.get_token("github")` (same
convention every other Reach channel uses); anonymous REST calls still work,
just at the lower unauthenticated rate limit. When REST is unreachable
(offline, no token on a private repo) each call falls back to the `gh` CLI
if it is on PATH, and raises a plain :class:`CiFailuresError` naming both
failures when neither works.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.reach import credentials
from src.reach.http_client import make_client

logger = logging.getLogger(__name__)

_API = "https://api.github.com"
_MAX_CACHE_RUNS = 30
_MAX_BLOCKS_PER_JOB = 60
_EXCERPT_MAX_LINES = 40
_GH_TIMEOUT_S = 25

# ---------------------------------------------------------------------------
# Remote parsing
# ---------------------------------------------------------------------------
# git@alias:Owner/Repo.git (an SSH host alias, not necessarily "github.com")
# and the same shape without a `.git` suffix.
_SSH_RE = re.compile(r"^git@(?P<host>[\w.\-]+):(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+?)(?:\.git)?/?$")
# https://github.com/Owner/Repo(.git)? -- also matches ssh:// / git:// forms.
_HTTPS_RE = re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?[^/]+/(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+?)(?:\.git)?/?$")


class CiFailuresError(RuntimeError):
    """The run/jobs/logs could not be reached: no usable token, `gh` CLI
    missing or unauthenticated, or no network -- always a message a human
    can act on, never a bare exception repr."""


def repo_from_workspace(workspace: str) -> Optional[Tuple[str, str]]:
    """``(owner, repo)`` from ``git remote get-url origin`` in `workspace`,
    or ``None`` when there is no repo, no `origin`, or the URL is not one of
    the shapes above."""
    ws = str(workspace or "").strip()
    if not ws or not os.path.isdir(ws):
        return None
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=ws,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ci_failures: git remote lookup failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    if not url:
        return None
    m = _SSH_RE.match(url)
    if m:
        return m.group("owner"), m.group("repo")
    m = _HTTPS_RE.match(url)
    if m:
        return m.group("owner"), m.group("repo")
    return None


# ---------------------------------------------------------------------------
# GitHub REST, with a `gh` CLI fallback
# ---------------------------------------------------------------------------

def _headers(token: Optional[str]) -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = token if token is not None else credentials.get_token("github")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _gh_path() -> Optional[str]:
    return shutil.which("gh")


def _gh_json(cmd: List[str]) -> Optional[Any]:
    path = _gh_path()
    if not path:
        return None
    try:
        proc = subprocess.run([path, *cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_GH_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ci_failures: gh CLI call failed: %s", exc)
        return None
    if proc.returncode != 0:
        logger.debug("ci_failures: gh CLI exit %s: %s", proc.returncode, (proc.stderr or "")[:300])
        return None
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        return None


async def list_runs(owner: str, repo: str, *, branch: Optional[str] = None,
                     status: Optional[str] = "failure", limit: int = 10,
                     token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Workflow runs for `owner/repo`, newest first. `status=None` returns
    runs of any conclusion (used when a caller names an exact `run_id`)."""
    headers = _headers(token)
    params: Dict[str, Any] = {"per_page": max(1, min(int(limit or 10), 100))}
    if status:
        params["status"] = status
    if branch:
        params["branch"] = branch
    url = f"{_API}/repos/{owner}/{repo}/actions/runs"
    rest_err = None
    try:
        async with make_client(headers=headers) as client:
            resp = await client.get(url, params=params)
        if resp.status_code < 400:
            return list((resp.json() or {}).get("workflow_runs") or [])
        rest_err = f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        rest_err = str(exc)
    cmd = ["run", "list", "-R", f"{owner}/{repo}", "--limit", str(max(1, min(int(limit or 10), 100))),
           "--json", "databaseId,status,conclusion,headBranch,displayTitle,createdAt,headSha,event,url"]
    if status:
        cmd += ["--status", status]
    if branch:
        cmd += ["--branch", branch]
    rows = _gh_json(cmd)
    if rows is not None:
        return [_gh_run_to_rest(r) for r in rows]
    raise CiFailuresError(
        f"could not list runs for {owner}/{repo} via the GitHub REST API ({rest_err}) and "
        f"the `gh` CLI is not installed or not authenticated"
    )


async def _get_run(owner: str, repo: str, run_id: Any, *, token: Optional[str] = None) -> Dict[str, Any]:
    headers = _headers(token)
    url = f"{_API}/repos/{owner}/{repo}/actions/runs/{run_id}"
    rest_err = None
    try:
        async with make_client(headers=headers) as client:
            resp = await client.get(url)
        if resp.status_code < 400:
            return resp.json() or {}
        rest_err = f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        rest_err = str(exc)
    row = _gh_json(["run", "view", str(run_id), "-R", f"{owner}/{repo}", "--json",
                     "databaseId,status,conclusion,headBranch,displayTitle,createdAt,headSha,event,url"])
    if row is not None:
        return _gh_run_to_rest(row)
    raise CiFailuresError(
        f"could not fetch run {run_id} for {owner}/{repo} via the GitHub REST API ({rest_err}) "
        f"and the `gh` CLI is not installed or not authenticated"
    )


def _gh_run_to_rest(row: Dict[str, Any]) -> Dict[str, Any]:
    """`gh run list/view --json` field names -> the REST response's own
    field names, so every downstream reader (analyze/summary_md/the route)
    sees ONE shape regardless of which backend answered."""
    return {
        "id": row.get("databaseId"), "status": row.get("status"), "conclusion": row.get("conclusion"),
        "head_branch": row.get("headBranch"), "display_title": row.get("displayTitle"),
        "created_at": row.get("createdAt"), "head_sha": row.get("headSha"),
        "event": row.get("event"), "html_url": row.get("url"),
    }


async def list_jobs(owner: str, repo: str, run_id: Any, *, token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Jobs for a run, newest first as GitHub returns them."""
    headers = _headers(token)
    url = f"{_API}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    rest_err = None
    try:
        async with make_client(headers=headers) as client:
            resp = await client.get(url, params={"per_page": 100})
        if resp.status_code < 400:
            return list((resp.json() or {}).get("jobs") or [])
        rest_err = f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        rest_err = str(exc)
    row = _gh_json(["run", "view", str(run_id), "-R", f"{owner}/{repo}", "--json", "jobs"])
    if isinstance(row, dict) and isinstance(row.get("jobs"), list):
        return [
            {"id": j.get("databaseId"), "name": j.get("name"), "conclusion": j.get("conclusion"),
             "status": j.get("status")}
            for j in row["jobs"]
        ]
    raise CiFailuresError(
        f"could not list jobs for run {run_id} via the GitHub REST API ({rest_err}) and the "
        f"`gh` CLI is not installed or not authenticated"
    )


async def job_log(owner: str, repo: str, job_id: Any, *, token: Optional[str] = None) -> str:
    """Raw log text for one job. The REST endpoint answers with a redirect
    to a signed blob-storage URL -- `make_client` follows redirects, so this
    is a single call either way."""
    headers = _headers(token)
    url = f"{_API}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
    rest_err = None
    try:
        async with make_client(headers=headers) as client:
            resp = await client.get(url)
        if resp.status_code < 400:
            return resp.text
        rest_err = f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        rest_err = str(exc)
    path = _gh_path()
    if path:
        try:
            proc = subprocess.run(
                [path, "run", "view", "-R", f"{owner}/{repo}", "--log", "--job", str(job_id)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_GH_TIMEOUT_S,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception as exc:  # noqa: BLE001
            logger.debug("ci_failures: gh run view --log failed: %s", exc)
    raise CiFailuresError(
        f"could not fetch the log for job {job_id} via the GitHub REST API ({rest_err}) and "
        f"the `gh` CLI is not installed or not authenticated -- configure a GitHub token "
        f"(reach_github_token) or install/auth `gh` to read job logs"
    )


# ---------------------------------------------------------------------------
# Log cleaning + failure extraction
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s?", re.M)
#: `gh run view --log` prefixes every line with the job and the step,
#: tab-separated, before the timestamp ("test<TAB>Run pytest<TAB>2026-...Z
#: FAILED ..."). Left in place, no failure pattern (all anchored at the
#: start of the line) matched: seen live, a failing pytest run came back as
#: "0 distinct failures". Its output is UTF-8 (with a byte-order mark on
#: the first line); read with Windows' default code page it did not decode
#: at all, and the log looked unreadable.
_GH_PREFIX_RE = re.compile(r"^[^\t\n]*\t[^\t\n]*\t\ufeff?(?=\d{4}-\d{2}-\d{2}T\d{2}:)", re.M)


def _clean(text: str) -> str:
    text = _ANSI_RE.sub("", (text or "").replace("\ufeff", ""))
    text = _GH_PREFIX_RE.sub("", text)
    text = _TS_RE.sub("", text)
    return text


@dataclass
class FailureBlock:
    kind: str
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    test: Optional[str] = None
    excerpt: str = ""

    def dedupe_key(self) -> Tuple[str, str, str, str]:
        return (self.kind, self.file or "", self.test or "", (self.message or "").strip()[:160])


_PYTEST_RE = re.compile(r"^FAILED\s+(?P<file>[^:\s]+)::(?P<test>[^\s]+?)(?:\s*-\s*(?P<msg>.*))?$")
_JEST_RE = re.compile(r"^●\s+(?P<test>.+)$")
_TSC_RE = re.compile(r"^(?P<file>[^\s():]+)\((?P<line>\d+),\d+\):\s*error\s+(?P<code>TS\d+):\s*(?P<msg>.+)$")
_ESLINT_FILE_RE = re.compile(r"^(?P<file>[\w./\-]+\.(?:js|jsx|ts|tsx|mjs|cjs))$")
_ESLINT_LINE_RE = re.compile(r"^(?P<line>\d+):\d+\s+error\s+(?P<msg>.+?)\s+(?P<rule>[\w-]+(?:/[\w-]+)?)$")
_CARGO_RE = re.compile(r"^error(?:\[(?P<code>E\d+)\])?:\s*(?P<msg>.+)$")
_CARGO_LOC_RE = re.compile(r"^-->\s*(?P<file>[^:]+):(?P<line>\d+):\d+")
# cargo's own trailing summary line ("error: aborting due to N previous
# errors") matches the generic `error: ...` starter but names no new
# diagnostic -- never a block of its own.
_CARGO_SUMMARY_RE = re.compile(r"^aborting due to( \d+)? previous errors?", re.I)
_GO_FAIL_RE = re.compile(r"^--- FAIL:\s*(?P<test>\S+)\s*\(")
_NPM_ERR_RE = re.compile(r"^npm ERR!\s*(?P<msg>.+)$")
_GH_ANNOT_RE = re.compile(r"^##\[error\](?P<msg>.+)$")
_TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\):\s*$")
_GENERIC_ERROR_RE = re.compile(r"^(?:Error|ERROR):\s*(?P<msg>.+)$")

# Order matters only for which "starter" wins when a line is ambiguous;
# ESLint is handled separately (its lines need the file line above them).
_STARTERS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("pytest", _PYTEST_RE),
    ("jest", _JEST_RE),
    ("tsc", _TSC_RE),
    ("cargo", _CARGO_RE),
    ("go", _GO_FAIL_RE),
    ("npm", _NPM_ERR_RE),
    ("gh_annotation", _GH_ANNOT_RE),
    ("traceback", _TRACEBACK_RE),
    ("generic", _GENERIC_ERROR_RE),
)


def extract_failures(log_text: str) -> List[FailureBlock]:
    """Heuristic failure blocks out of one job's raw log text, deduped."""
    text = _clean(log_text)
    lines = text.splitlines()
    n = len(lines)
    blocks: List[FailureBlock] = []
    current_eslint_file: Optional[str] = None
    i = 0
    while i < n:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        # ESLint: a bare file path line immediately followed by "<line>:<col> error ..." lines.
        m_file = _ESLINT_FILE_RE.match(stripped)
        if m_file and i + 1 < n and _ESLINT_LINE_RE.match(lines[i + 1].strip()):
            current_eslint_file = m_file.group("file")
            i += 1
            continue
        m_es = _ESLINT_LINE_RE.match(stripped) if current_eslint_file else None
        if m_es:
            # One block per diagnostic line -- ESLint reports several
            # independent findings per file, not one block per file.
            blocks.append(FailureBlock(
                kind="eslint", file=current_eslint_file, line=int(m_es.group("line")),
                message=m_es.group("msg").strip(), excerpt=lines[i],
            ))
            i += 1
            continue

        matched = False
        for kind, rx in _STARTERS:
            m = rx.match(stripped)
            if not m:
                continue
            if kind == "cargo" and _CARGO_SUMMARY_RE.match((m.groupdict().get("msg") or "").strip()):
                matched = True
                i += 1
                break
            matched = True
            excerpt = [lines[i]]
            j = i + 1
            while j < n and len(excerpt) < _EXCERPT_MAX_LINES:
                nxt = lines[j].strip()
                if not nxt:
                    break
                if kind == "cargo":
                    excerpt.append(lines[j])
                    j += 1
                    if _CARGO_LOC_RE.match(nxt):
                        break
                    continue
                if any(rx2.match(nxt) for _, rx2 in _STARTERS):
                    break
                excerpt.append(lines[j])
                j += 1
            gd = m.groupdict()
            file_ = gd.get("file")
            line_no: Optional[int] = None
            if kind == "cargo":
                for exline in excerpt:
                    lm = _CARGO_LOC_RE.match(exline.strip())
                    if lm:
                        file_, line_no = lm.group("file"), int(lm.group("line"))
                        break
            elif gd.get("line"):
                try:
                    line_no = int(gd["line"])
                except (TypeError, ValueError):
                    line_no = None
            message = (gd.get("msg") or stripped).strip()
            blocks.append(FailureBlock(
                kind=kind, file=file_, line=line_no, test=gd.get("test"),
                message=message, excerpt="\n".join(excerpt),
            ))
            i = j
            break
        if not matched:
            i += 1
    return dedupe_blocks(blocks)


def dedupe_blocks(blocks: Sequence[FailureBlock]) -> List[FailureBlock]:
    seen = set()
    out: List[FailureBlock] = []
    for b in blocks:
        key = b.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out


def dedupe_dicts(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in rows:
        key = (r.get("kind"), r.get("file") or "", r.get("test") or "", (r.get("message") or "").strip()[:160])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Workspace mapping: resolved path, last-touch, code-graph area (best effort)
# ---------------------------------------------------------------------------

def _last_touched(workspace: str, rel_path: str) -> Optional[Dict[str, str]]:
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%an|%ad", "--", rel_path],
            cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ci_failures: git log lookup failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not out or "|" not in out:
        return None
    author, date = out.split("|", 1)
    author, date = author.strip(), date.strip()
    if not author and not date:
        return None
    return {"author": author, "date": date}


def _code_graph_area(workspace: str, rel_path: str) -> Optional[str]:
    """The level-0 community name/id `rel_path` belongs to, read directly
    from whatever `src.code_graph.communities` already computed for this
    workspace -- never triggers a build here, so this stays cheap. Returns
    `None` on any failure (module unavailable, nothing indexed yet, table
    missing): this is decoration, never a reason to fail the analysis."""
    try:
        from src.code_graph import communities as _comm
        from src.context_engine import store as ce_store
        fp = _comm._fingerprint(workspace, "")
        where, params = _comm._where(workspace, "", extra="fingerprint = ? AND path = ?")
        params = [*params, fp, rel_path]
        with ce_store.db() as conn:
            row = conn.execute(
                f"SELECT community_id FROM code_community_files WHERE {where}", params
            ).fetchone()
            if not row or not row[0]:
                return None
            cid = row[0]
            where2, params2 = _comm._where(workspace, "", extra="fingerprint = ? AND level = 0 AND id = ?")
            params2 = [*params2, fp, cid]
            crow = conn.execute(f"SELECT name FROM code_communities WHERE {where2}", params2).fetchone()
            return (crow[0] if crow and crow[0] else cid)
    except Exception as exc:  # noqa: BLE001 - best effort, never raise
        logger.debug("ci_failures: code graph lookup skipped: %s", exc)
        return None


def map_to_workspace(blocks: Sequence[Any], workspace: str) -> List[Dict[str, Any]]:
    """`FailureBlock`s or dicts -> plain dicts with `resolved_path`,
    `last_touched` and `community` filled in, best effort."""
    ws = str(workspace or "").strip()
    out: List[Dict[str, Any]] = []
    for b in blocks:
        row = dict(b) if isinstance(b, dict) else asdict(b)
        resolved = None
        candidate = (row.get("file") or "").strip()
        if candidate and ws:
            cand = candidate.lstrip("./").replace("\\", "/")
            full = os.path.normpath(os.path.join(ws, cand))
            if os.path.commonpath([os.path.abspath(ws), os.path.abspath(full)]) == os.path.abspath(ws) \
                    and os.path.isfile(full):
                resolved = os.path.relpath(full, ws).replace("\\", "/")
        row["resolved_path"] = resolved
        row["last_touched"] = _last_touched(ws, resolved) if (resolved and ws) else None
        row["community"] = _code_graph_area(ws, resolved) if (resolved and ws) else None
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Cache: <DATA_DIR>/ci_failures/<owner>/<run_id>.json, last 30 kept
# ---------------------------------------------------------------------------

def _cache_dir(owner: str) -> str:
    from src.constants import DATA_DIR
    safe_owner = re.sub(r"[^\w.\-]", "_", str(owner or "unknown"))[:80] or "unknown"
    d = os.path.join(DATA_DIR, "ci_failures", safe_owner)
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(owner: str, run_id: Any) -> str:
    return os.path.join(_cache_dir(owner), f"{run_id}.json")


def _cache_write(owner: str, run_id: Any, payload: Dict[str, Any]) -> None:
    try:
        with open(_cache_path(owner, run_id), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        _rotate_cache(owner)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ci_failures: cache write failed: %s", exc)


def _cache_read(owner: str, run_id: Any) -> Optional[Dict[str, Any]]:
    path = _cache_path(owner, run_id)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ci_failures: cache read failed: %s", exc)
        return None


def _rotate_cache(owner: str, keep: int = _MAX_CACHE_RUNS) -> None:
    try:
        d = _cache_dir(owner)
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for stale in files[keep:]:
            try:
                os.remove(stale)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("ci_failures: cache rotation failed: %s", exc)


# ---------------------------------------------------------------------------
# analyze() / propose_fixes()
# ---------------------------------------------------------------------------

@dataclass
class Analysis:
    owner: str
    repo: str
    run: Dict[str, Any]
    jobs: List[Dict[str, Any]]
    blocks: List[Dict[str, Any]]
    workspace: str = ""
    from_cache: bool = False

    def summary_md(self) -> str:
        run_id = self.run.get("id", "?")
        lines = [f"# CI failure analysis -- {self.owner}/{self.repo} run {run_id}",
                 f"branch: {self.run.get('head_branch') or '?'} | "
                 f"conclusion: {self.run.get('conclusion') or '?'}"]
        failed_jobs = [j for j in self.jobs if j.get("conclusion") not in ("success", "skipped", "neutral")]
        lines.append("")
        lines.append(f"{len(failed_jobs)} job(s) not green, {len(self.blocks)} distinct failure(s).")
        for j in failed_jobs:
            lines.append(f"\n## {j.get('name') or 'job'} ({j.get('conclusion') or '?'})")
            for b in self.blocks:
                if b.get("job") and b.get("job") != j.get("name"):
                    continue
                loc = b.get("resolved_path") or b.get("file") or "?"
                if b.get("line"):
                    loc = f"{loc}:{b['line']}"
                lines.append(f"- **[{b.get('kind')}]** {loc} -- {(b.get('message') or '')[:200]}")
                touch = b.get("last_touched")
                if touch:
                    lines.append(f"  last touched by {touch.get('author')} on {touch.get('date')}")
                if b.get("community"):
                    lines.append(f"  area: {b['community']}")
        if not failed_jobs:
            for b in self.blocks:
                loc = b.get("resolved_path") or b.get("file") or "?"
                lines.append(f"- **[{b.get('kind')}]** {loc} -- {(b.get('message') or '')[:200]}")
        return "\n".join(lines)


async def analyze(workspace: str, *, run_id: Optional[Any] = None, branch: Optional[str] = None,
                   token: Optional[str] = None) -> Analysis:
    repo_id = repo_from_workspace(workspace)
    if not repo_id:
        raise CiFailuresError(
            "could not determine an owner/repo from this workspace's `git remote get-url origin`"
        )
    owner, repo = repo_id
    if run_id:
        run = await _get_run(owner, repo, run_id, token=token)
    else:
        runs = await list_runs(owner, repo, branch=branch, status="failure", limit=5, token=token)
        run = runs[0] if runs else None
    if not run:
        raise CiFailuresError(
            f"no failed run found for {owner}/{repo}" + (f" on branch {branch}" if branch else "")
        )
    rid = run.get("id") or run_id
    cached = _cache_read(owner, rid)
    if cached is not None:
        jobs = cached.get("jobs") or []
        blocks_raw = cached.get("blocks") or []
        from_cache = True
    else:
        jobs = await list_jobs(owner, repo, rid, token=token)
        blocks_raw: List[Dict[str, Any]] = []
        for j in jobs:
            if j.get("conclusion") not in ("failure", "timed_out", "cancelled"):
                continue
            try:
                log_text = await job_log(owner, repo, j.get("id"), token=token)
            except CiFailuresError as exc:
                logger.info("ci_failures: could not read log for job %s: %s", j.get("id"), exc)
                continue
            for b in extract_failures(log_text)[:_MAX_BLOCKS_PER_JOB]:
                d = asdict(b)
                d["job"] = j.get("name")
                blocks_raw.append(d)
        blocks_raw = dedupe_dicts(blocks_raw)
        _cache_write(owner, rid, {"run": run, "jobs": jobs, "blocks": blocks_raw, "cached_at": time.time()})
        from_cache = False
    mapped = map_to_workspace(blocks_raw, workspace)
    return Analysis(owner=owner, repo=repo, run=run, jobs=jobs, blocks=mapped,
                     workspace=workspace, from_cache=from_cache)


def _build_fix_prompt(analysis: Analysis) -> str:
    lines = [
        "You are triaging failed CI jobs. For each failure below, give the most likely "
        "root cause and a concrete fix. Reply with ONLY a JSON array of objects, each "
        "{\"file\": ..., \"cause\": ..., \"fix\": ..., \"confidence\": <0-1 float>}.",
        "",
    ]
    for b in analysis.blocks[:20]:
        loc = b.get("resolved_path") or b.get("file") or "?"
        lines.append(f"- [{b.get('kind')}] {loc}:{b.get('line') or '?'} -- {(b.get('message') or '')[:300]}")
        excerpt = b.get("excerpt") or ""
        if excerpt:
            snippet = " | ".join(excerpt.splitlines()[:6])
            lines.append(f"  excerpt: {snippet[:500]}")
    return "\n".join(lines)


def _parse_fix_response(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, tuple):
        raw = raw[0] if raw else ""
    if not isinstance(raw, str) or not raw.strip():
        return []
    text = raw.strip()
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in data[:20]:
        if not isinstance(item, dict):
            continue
        confidence = item.get("confidence")
        out.append({
            "file": str(item.get("file") or "")[:400],
            "cause": str(item.get("cause") or "")[:1000],
            "fix": str(item.get("fix") or "")[:1000],
            "confidence": confidence if isinstance(confidence, (int, float)) else None,
        })
    return out


async def propose_fixes(analysis: Analysis, owner: str = "", model: Optional[str] = None) -> List[Dict[str, Any]]:
    """A ranked {file, cause, fix, confidence} guess per failure, from the
    configured utility model. Without a usable endpoint/model, returns the
    mapping alone (each entry with `cause`/`fix`/`confidence` set to
    `None` and a `note`) rather than raising -- this is an enrichment, and
    its absence must never look like the analysis itself failed."""
    if not analysis.blocks:
        return []
    url = model_name = headers = None
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model_name, headers = resolve_endpoint("utility", owner=owner or None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ci_failures: endpoint resolution failed: %s", exc)
    use_model = model or model_name
    if not url or not use_model:
        return [
            {"file": b.get("resolved_path") or b.get("file"), "cause": None, "fix": None,
             "confidence": None, "note": "no utility model configured"}
            for b in analysis.blocks
        ]
    try:
        from src.llm_core import llm_call_async
        from src import mode_effort as _me
        _ci_overrides = _me.for_mode("ci_analysis")
        raw = await llm_call_async(
            url=url, model=use_model, messages=[{"role": "user", "content": _build_fix_prompt(analysis)}],
            headers=headers, temperature=0.2, max_tokens=1500,
            timeout=int(_me.timeout_for(_ci_overrides, 60)),
            max_retries=1, workload="background", gen_overrides=_ci_overrides,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ci_failures: propose_fixes model call failed: %s", exc)
        return []
    return _parse_fix_response(raw)
