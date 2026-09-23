"""src/prior_art.py — reuse, adapt, or write: verified prior-art decisions.

WHY: before an agent starts building something, it should know which parts
of the idea already exist as maintained open-source projects (`reuse`, as a
dependency), which exist only as reference implementations worth studying
(`adapt` — copy the approach, never the code), and which are small or
generic enough to just write. Models recall repository names badly — they
invent plausible-looking ones or cite dead projects — so this module never
lets a repository name reach the user unverified: every `owner/name` in a
slate is checked live against the GitHub API before it is reported back.

The model does the thinking (`rubric` only hands it a decomposition
checklist and the JSON shape to fill in); Faustus does the verification
(`verify`) and, when the model has no candidate at all, the search
(`search`). This is an original implementation for Faustus: no code was
copied from any existing "prior art" tool.

Three functions, no model calls anywhere in this module (deterministic
first — GIT-08):

* `rubric(idea, ...)`      — the decomposition checklist + slate JSON shape.
* `verify(slate, ...)`     — checks every repo in a filled slate live.
* `search(query, ...)`     — GitHub repository search, for when the model
                              has no candidate to verify.

Plus report persistence (`save_report`/`report`/`reports`) so a `verify`
call's evidence survives the turn that made it.

Network policy: this module hits exactly one host, `api.github.com`, and
only for repository metadata / search — read-only. It is gated the same way
`services/search/core.py` gates `web_search` on the `search_provider`
setting: `prior_art_enabled` (default True) turns the whole surface off
without any network attempt, returning an honest `unverified` result. A
real network failure (no route, timeout, DNS) degrades the same way, repo
by repo, never raising into the caller.

Caching: repository metadata is cached in a small SQLite store
(`DATA_DIR/prior_art.sqlite3`) for 24h, so repeated `verify` calls on
overlapping slates don't burn the unauthenticated 60 req/h GitHub quota.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
_USER_AGENT = "Faustus-PriorArt/1.0"
_REQUEST_TIMEOUT_S = 8.0
_CACHE_TTL = timedelta(hours=24)
_MAX_REPOS = 40
_MAX_WORKERS = 8
_DB_FILENAME = "prior_art.sqlite3"
_SCHEMA_VERSION = 1

_REPO_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\s*$")

_VERDICTS = ("reuse", "adapt", "write")

# ── license matrix ──────────────────────────────────────────────────────────
# SPDX ids, normalized (strip "-only"/"-or-later", uppercase compared case-
# insensitively). Anything not listed here — including GitHub's own
# "NOASSERTION" and a null license — is treated as "none": no license found
# means default copyright ("all rights reserved"), which is never safe to
# copy from and is reported as such.
_PERMISSIVE = {
    "MIT", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "BSD-3-CLAUSE-CLEAR", "BSD-4-CLAUSE",
    "APACHE-2.0", "ISC", "ZLIB", "UNLICENSE", "0BSD", "CC0-1.0", "WTFPL",
    "BSL-1.0", "PYTHON-2.0", "PSF-2.0",
}
_WEAK_COPYLEFT = {
    "LGPL-2.0", "LGPL-2.1", "LGPL-3.0", "MPL-1.1", "MPL-2.0",
    "EPL-1.0", "EPL-2.0", "CDDL-1.0", "CDDL-1.1",
}
_STRONG_COPYLEFT = {"GPL-1.0", "GPL-2.0", "GPL-3.0", "AGPL-1.0", "AGPL-3.0"}


def _license_category(spdx_id: Optional[str]) -> str:
    """`permissive` / `weak_copyleft` / `strong_copyleft` / `none`."""
    if not spdx_id:
        return "none"
    norm = spdx_id.strip().upper()
    norm = re.sub(r"-(ONLY|OR-LATER)$", "", norm)
    if norm in ("NOASSERTION", "OTHER", ""):
        return "none"
    if norm in _PERMISSIVE:
        return "permissive"
    if norm in _WEAK_COPYLEFT:
        return "weak_copyleft"
    if norm in _STRONG_COPYLEFT:
        return "strong_copyleft"
    return "none"


def license_compatibility(target_category: str, candidate_category: str, verdict: str) -> Dict[str, Any]:
    """Compatibility of using `candidate_category` for `verdict` in a project
    licensed under `target_category`. Never invents a verdict on the license
    itself — only flags when the combination needs a human's attention."""
    if verdict == "adapt":
        note = "adapt: study the approach, write your own code — no code is copied, so the source's license does not attach to yours."
        if candidate_category == "none":
            note += " No license was found on the source: read it for understanding, never copy text or code from it."
        return {"compatible": True, "flag": False, "note": note}

    if verdict == "write":
        return {"compatible": True, "flag": False,
                "note": "write: no dependency and no reference copy, so no license question applies."}

    # verdict == "reuse": the candidate becomes a dependency of the target
    # project, so its license terms apply to the combined work.
    if candidate_category == "permissive":
        return {"compatible": True, "flag": False,
                "note": "reuse: permissive license, safe to depend on from any project."}
    if candidate_category == "none":
        return {"compatible": False, "flag": True,
                "note": "reuse: no license found (\"all rights reserved\" by default) — this is not safe to "
                        "depend on. Confirm a license exists before adding it, or downgrade to adapt "
                        "(study the approach, write your own code) or write."}
    if candidate_category == "weak_copyleft":
        flag = target_category == "strong_copyleft"
        return {"compatible": not flag, "flag": flag,
                "note": "reuse: weak-copyleft (LGPL/MPL/EPL-style) — fine as an unmodified dependency for "
                        "most projects; check the exact terms if you plan to modify and redistribute it."}
    # strong_copyleft candidate
    flag = target_category in ("permissive", "weak_copyleft", "none", "")
    return {"compatible": not flag, "flag": flag,
            "note": ("reuse: strong-copyleft (GPL/AGPL-style) dependency of a permissive/weak-copyleft "
                     "project — this typically obligates the whole combined work under the same terms. "
                     "Downgrade to adapt (study the approach, write your own code) unless the target "
                     "project's own license already matches.") if flag else
                    "reuse: strong-copyleft dependency of an already strong-copyleft project — check the "
                    "exact terms match, but no license family mismatch."}


# ── health / staleness ──────────────────────────────────────────────────────

def _health(pushed_at: Optional[str], *, now: Optional[datetime] = None) -> str:
    """`active` (<6mo), `slowing` (6-18mo), `stale` (>18mo), or `unknown`."""
    if not pushed_at:
        return "unknown"
    try:
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    age = now - pushed
    if age <= timedelta(days=182):
        return "active"
    if age <= timedelta(days=548):
        return "slowing"
    return "stale"


# ── settings / token ─────────────────────────────────────────────────────────

def _enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("prior_art_enabled", True))
    except Exception:  # noqa: BLE001 - settings must never block a read
        return True


def _resolve_token() -> str:
    """Settings secret first (mirrors `brave_api_key` etc.), then env.
    Never logged, never included in a report or table — only sent as the
    `Authorization` header of a request to `api.github.com`."""
    try:
        from src.settings import get_setting
        token = str(get_setting("prior_art_github_token", "") or "").strip()
        if token:
            return token
    except Exception:  # noqa: BLE001
        pass
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT,
               "X-GitHub-Api-Version": "2022-11-28"}
    token = _resolve_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ── sqlite cache + report store ──────────────────────────────────────────────

def _db_path() -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, _DB_FILENAME)


_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    path = path or _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass  # a filesystem that refuses WAL still works with the default journal
    with _INIT_LOCK:
        if path not in _INITIALIZED_PATHS:
            _ensure_schema(conn)
            _INITIALIZED_PATHS.add(path)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS repo_cache ("
        " full_name TEXT PRIMARY KEY,"
        " fetched_at TEXT NOT NULL,"
        " status TEXT NOT NULL,"
        " data TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reports ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " id TEXT NOT NULL UNIQUE,"
        " owner TEXT NOT NULL DEFAULT '',"
        " project_id TEXT NOT NULL DEFAULT '',"
        " idea TEXT NOT NULL DEFAULT '',"
        " slate TEXT NOT NULL,"
        " results TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    conn.commit()


def _cache_get(conn: sqlite3.Connection, full_name: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        "SELECT fetched_at, status, data FROM repo_cache WHERE full_name = ?",
        (full_name.lower(),),
    ).fetchone()
    if not row:
        return None
    try:
        fetched_at = datetime.fromisoformat(row["fetched_at"])
    except ValueError:
        return None
    if datetime.now(timezone.utc) - fetched_at > _CACHE_TTL:
        return None
    try:
        return json.loads(row["data"])
    except json.JSONDecodeError:
        return None


def _cache_put(conn: sqlite3.Connection, full_name: str, status: str, data: Dict[str, Any]) -> None:
    try:
        conn.execute(
            "INSERT INTO repo_cache (full_name, fetched_at, status, data) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(full_name) DO UPDATE SET fetched_at=excluded.fetched_at,"
            " status=excluded.status, data=excluded.data",
            (full_name.lower(), datetime.now(timezone.utc).isoformat(), status, json.dumps(data)),
        )
        conn.commit()
    except sqlite3.Error as exc:  # noqa: BLE001 - caching is an optimization, never load-bearing
        logger.warning("prior_art: cache write failed for %s: %s", full_name, exc)


def _next_report_id(conn: sqlite3.Connection) -> str:
    cur = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM reports")
    seq = cur.fetchone()[0]
    return f"PA-{seq:06d}"


def save_report(idea: str, slate: Dict[str, Any], results: Dict[str, Any], *,
                owner: str = "", project_id: str = "") -> str:
    conn = _connect()
    report_id = _next_report_id(conn)
    conn.execute(
        "INSERT INTO reports (id, owner, project_id, idea, slate, results, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (report_id, owner, project_id, idea or "", json.dumps(slate), json.dumps(results),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return report_id


def report(report_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    row = conn.execute(
        "SELECT id, owner, project_id, idea, slate, results, created_at FROM reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"], "owner": row["owner"], "project_id": row["project_id"],
        "idea": row["idea"], "slate": json.loads(row["slate"]),
        "results": json.loads(row["results"]), "created_at": row["created_at"],
    }


def reports(limit: int = 20, *, owner: str = "") -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 200))
    conn = _connect()
    if owner:
        rows = conn.execute(
            "SELECT id, owner, project_id, idea, created_at FROM reports"
            " WHERE owner = ? ORDER BY seq DESC LIMIT ?",
            (owner, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, owner, project_id, idea, created_at FROM reports ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


# ── GitHub API ────────────────────────────────────────────────────────────

def _parse_repo_ref(ref: str) -> Optional[Tuple[str, str]]:
    match = _REPO_RE.match(ref or "")
    if not match:
        return None
    return match.group(1), match.group(2)


def _fetch_repo(client: httpx.Client, owner: str, name: str) -> Dict[str, Any]:
    """One `GET /repos/{owner}/{name}`, classified. Never raises — a network
    or parsing failure comes back as an `unverified` status, same discipline
    `services/search/providers.py` uses for every provider call."""
    requested = f"{owner}/{name}"
    try:
        resp = client.get(f"{GITHUB_API}/repos/{owner}/{name}")
    except httpx.RequestError as exc:
        return {"requested": requested, "status": "unverified",
                "reason": f"network error: {exc}"}

    remaining = resp.headers.get("X-RateLimit-Remaining")
    reset = resp.headers.get("X-RateLimit-Reset")

    if resp.status_code == 404:
        return {"requested": requested, "status": "missing",
                "reason": "no such repository"}
    if resp.status_code == 403 and remaining == "0":
        return {"requested": requested, "status": "rate_limited",
                "reason": "GitHub API rate limit exhausted",
                "rate_limit_reset": reset}
    if resp.status_code != 200:
        return {"requested": requested, "status": "unverified",
                "reason": f"GitHub API returned HTTP {resp.status_code}"}

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        return {"requested": requested, "status": "unverified",
                "reason": f"could not parse GitHub API response: {exc}"}

    full_name = data.get("full_name") or requested
    license_info = data.get("license") or {}
    result = {
        "requested": requested,
        "full_name": full_name,
        "renamed": full_name.lower() != requested.lower(),
        "status": "archived" if data.get("archived") else ("fork" if data.get("fork") else "found"),
        "archived": bool(data.get("archived")),
        "fork": bool(data.get("fork")),
        "description": (data.get("description") or "")[:280],
        "stars": data.get("stargazers_count", 0),
        "open_issues": data.get("open_issues_count", 0),
        "pushed_at": data.get("pushed_at"),
        "default_branch": data.get("default_branch"),
        "homepage": data.get("homepage") or "",
        "html_url": data.get("html_url") or f"https://github.com/{full_name}",
        "license_spdx": license_info.get("spdx_id"),
        "license_category": _license_category(license_info.get("spdx_id")),
        "health": _health(data.get("pushed_at")),
    }
    if remaining is not None:
        result["rate_limit_remaining"] = remaining
    return result


def _fetch_many(refs: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """`{full_name.lower(): result}` for every distinct, well-formed
    `owner/name` in `refs`, cache-first, else fetched in parallel within
    `_REQUEST_TIMEOUT_S` total. Malformed refs come back `invalid`."""
    seen: "dict[str, Tuple[str, str]]" = {}
    out: Dict[str, Dict[str, Any]] = {}
    for ref in refs:
        key = (ref or "").strip().lower()
        if not key or key in seen:
            continue
        parsed = _parse_repo_ref(ref)
        if not parsed:
            out[key] = {"requested": ref, "status": "invalid",
                        "reason": "expected 'owner/name'"}
            continue
        seen[key] = parsed
        if len(seen) >= _MAX_REPOS:
            break

    conn = _connect()
    to_fetch: List[Tuple[str, str, str]] = []
    for key, (owner, name) in seen.items():
        cached = _cache_get(conn, key)
        if cached is not None:
            out[key] = {**cached, "cached": True}
        else:
            to_fetch.append((key, owner, name))

    if to_fetch:
        try:
            with httpx.Client(timeout=_REQUEST_TIMEOUT_S, follow_redirects=True,
                              headers=_headers()) as client:
                with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(to_fetch))) as pool:
                    futures = {
                        pool.submit(_fetch_repo, client, owner, name): key
                        for key, owner, name in to_fetch
                    }
                    for future in as_completed(futures, timeout=_REQUEST_TIMEOUT_S + 2):
                        key = futures[future]
                        try:
                            result = future.result()
                        except Exception as exc:  # noqa: BLE001
                            result = {"requested": key, "status": "unverified",
                                     "reason": f"{type(exc).__name__}: {exc}"}
                        out[key] = result
                        # Cache real answers (including a confirmed 404) so a
                        # repeated verify of the same slate costs no quota;
                        # never cache a transient failure (rate limit,
                        # network error) as if it were durable.
                        if result.get("status") in ("found", "archived", "fork", "missing"):
                            _cache_put(conn, key, result["status"], result)
        except TimeoutError:
            pass  # whatever didn't complete falls through to the loop below
        except Exception as exc:  # noqa: BLE001 - e.g. no network at all
            logger.warning("prior_art: batch fetch failed: %s", exc)
            for key, _, _ in to_fetch:
                out.setdefault(key, {"requested": key, "status": "unverified",
                                     "reason": f"network unavailable: {exc}"})
        for key, _, _ in to_fetch:
            out.setdefault(key, {"requested": key, "status": "unverified",
                                 "reason": "timed out before a response arrived"})
    return out


# ── rubric ───────────────────────────────────────────────────────────────

_SLATE_SHAPE_EXAMPLE = {
    "idea": "<the idea, restated in one line>",
    "components": [
        {
            "name": "<component, e.g. 'HTML/CSS layout parsing'>",
            "verdict": "reuse | adapt | write",
            "repos": ["owner/name", "owner2/name2"],
            "rationale": "<why this verdict, in one or two sentences>",
        }
    ],
}


def rubric(idea: str, *, stack: str = "", license: str = "", constraints: str = "") -> Dict[str, Any]:
    """The decomposition rubric the agent must follow before building `idea`.

    Returns text for the model's own reasoning (English — the model's
    working language for tool instructions in this codebase) plus the
    structured slate shape it must fill in and hand to `verify`. No network
    call, no model call: this is a checklist, not a decision."""
    idea = (idea or "").strip()
    stack = (stack or "").strip()
    license = (license or "").strip()
    constraints = (constraints or "").strip()

    lines = [
        f"Decompose this idea into 3-10 concrete components: {idea or '(no idea given)'}",
        "",
        "For EACH component, pick exactly one verdict:",
        "  reuse — a maintained dependency. Pick this when correctness is hard-won: "
        "parsers, crypto, protocols, numerical algorithms, database engines, format "
        "codecs, compression, date/timezone handling, and similar. Do not reinvent these.",
        "  adapt — a reference implementation worth studying. Copy the APPROACH, never "
        "the code. Mandatory whenever the best candidate's license is incompatible with "
        "yours (see the license note `verify` will attach) or when the component is "
        "novel enough that no dependency fits but an existing project solved the same "
        "shape of problem.",
        "  write — small, generic, glue code, or a dependency would cost more (API "
        "surface, transitive deps, security surface) than it saves.",
        "",
        "For every `reuse` or `adapt` component, list 1-3 candidate repositories as "
        "`owner/name` (GitHub) — real ones you actually recall, not invented-sounding "
        "placeholders. Say briefly WHY each is a candidate. Do not present any "
        "repository to the user as existing until `prior_art verify` has confirmed it: "
        "you recall names imperfectly, and an unverified name may not exist or may be "
        "long dead.",
        "",
        "Then call `prior_art verify` with the filled slate below. Its report tells you "
        "which repos are real, maintained, and license-compatible, and will downgrade "
        "or replace a verdict when the evidence does not support it — follow that "
        "verdict, not your own first guess.",
        "",
        "Answer the user in the user's own language; everything above is your own "
        "working checklist, in English, not a message to relay verbatim.",
    ]
    if stack:
        lines.insert(1, f"Target stack/language: {stack}")
    if license:
        lines.insert(1, f"Target project license: {license} (checked for `reuse` candidates).")
    if constraints:
        lines.insert(1, f"Constraints: {constraints}")

    return {
        "idea": idea,
        "stack": stack,
        "license": license,
        "constraints": constraints,
        "instructions": "\n".join(lines),
        "verdicts": list(_VERDICTS),
        "slate_shape": _SLATE_SHAPE_EXAMPLE,
        "next_step": "prior_art verify {\"slate\": <the filled slate above>}",
    }


# ── verify ───────────────────────────────────────────────────────────────

def _normalize_component(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in _VERDICTS:
        verdict = "write"
    repos = raw.get("repos") or []
    if isinstance(repos, str):
        repos = [r.strip() for r in repos.split(",") if r.strip()]
    repos = [str(r) for r in repos if str(r or "").strip()][:3]
    rationale = str(raw.get("rationale") or "").strip()
    return {"name": name or "(unnamed component)", "verdict": verdict,
            "repos": repos, "rationale": rationale}


def _rank_key(entry: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Lower sorts first: verified+active+compatible candidates lead."""
    status = entry.get("status")
    status_rank = {"found": 0, "archived": 1, "fork": 1}.get(status, 3)
    health_rank = {"active": 0, "slowing": 1, "stale": 2, "unknown": 3}.get(entry.get("health"), 3)
    flag_rank = 1 if (entry.get("license") or {}).get("flag") else 0
    stars_rank = -int(entry.get("stars") or 0)
    return (status_rank, health_rank, flag_rank, stars_rank)


def _component_outcome(component: Dict[str, Any], ranked: List[Dict[str, Any]],
                       target_category: str) -> Tuple[str, List[str]]:
    verdict = component["verdict"]
    next_actions: List[str] = []
    if not component["repos"]:
        return "confirmed", next_actions  # write, or reuse/adapt with nothing to check yet

    viable = [r for r in ranked if r.get("status") in ("found", "archived", "fork")]
    if not viable:
        next_actions.append(f"{component['name']}: every candidate is missing — "
                            f"run `prior_art search` for alternatives, or write it.")
        return "replace", next_actions

    top = viable[0]
    if verdict == "adapt":
        if (top.get("license") or {}).get("candidate_category") == "none":
            next_actions.append(f"{component['name']}: no license on the reference — "
                                f"read it for understanding only, write your own code.")
        return "confirmed", next_actions

    if verdict == "write":
        return "confirmed", next_actions

    # verdict == "reuse"
    problems = []
    if top.get("status") in ("archived",):
        problems.append("archived")
    if top.get("health") == "stale":
        problems.append("stale (no push in 18+ months)")
    if (top.get("license") or {}).get("flag"):
        problems.append("license flagged")
    if problems:
        next_actions.append(f"{component['name']}: downgrading reuse to adapt "
                            f"({', '.join(problems)}) — study {top.get('full_name')}, "
                            f"write your own code, or `prior_art search` for a healthier "
                            f"alternative.")
        return "downgrade", next_actions
    return "confirmed", next_actions


def verify(slate: Any, *, target_license: str = "", stack: str = "",
          owner: str = "", project_id: str = "") -> Dict[str, Any]:
    """Verify every repo named in `slate` against the live GitHub API.

    `slate` = `{idea?, components: [{name, verdict, repos: [str], rationale}]}`.
    Returns the enriched slate, a rendered markdown table, next actions, and
    a saved report id. Never raises: a network problem is reported per repo
    as `unverified`, not an exception.

    `owner`/`project_id` are recorded on the saved report only (whose caller
    they are, for `reports(limit, owner=...)`) — they never change what gets
    verified or how.
    """
    if isinstance(slate, str):
        try:
            slate = json.loads(slate)
        except json.JSONDecodeError:
            slate = {}
    if not isinstance(slate, dict):
        slate = {}

    idea = str(slate.get("idea") or "").strip()
    raw_components = slate.get("components") or []
    if not isinstance(raw_components, list):
        raw_components = []
    components = [c for c in (_normalize_component(r) for r in raw_components) if c]

    target_category = _license_category(target_license) if target_license else "none"

    if not _enabled():
        results = {
            "verified": False, "reason": "prior_art is disabled (setting prior_art_enabled=false)",
            "rate_limited": False, "components": [
                {**c, "results": [{"requested": r, "status": "unverified",
                                   "reason": "prior_art is disabled"} for r in c["repos"]],
                 "outcome": "unverified", "next_actions": []}
                for c in components
            ],
        }
        results["table"] = _render_table(idea, results["components"])
        report_id = save_report(idea, slate, results, owner=owner, project_id=project_id)
        return {**results, "id": report_id, "exit_code": 0}

    all_refs = [r for c in components for r in c["repos"]]
    fetched = _fetch_many(all_refs)

    # "reachable" proves the GitHub API answered at all — a real 404/missing
    # or a rate-limit response counts (the request got there and back), only
    # a network-level failure (timeout, no route, DNS) does not. `verified`
    # tracks that, not "every repo was found": a slate can be fully verified
    # and still report every repo missing.
    reachable = False
    any_rate_limited = False
    enriched_components: List[Dict[str, Any]] = []
    for component in components:
        entries: List[Dict[str, Any]] = []
        for ref in component["repos"]:
            base = dict(fetched.get(ref.strip().lower(), {
                "requested": ref, "status": "unverified", "reason": "not fetched",
            }))
            status = base.get("status")
            if status == "rate_limited":
                any_rate_limited = True
            if status in ("found", "archived", "fork", "missing", "rate_limited"):
                reachable = True
            candidate_category = base.get("license_category", "none")
            if status in ("found", "archived", "fork"):
                base["license"] = {
                    "candidate_category": candidate_category,
                    **license_compatibility(target_category, candidate_category, component["verdict"]),
                }
            entries.append(base)
        ranked = sorted(entries, key=_rank_key)
        outcome, next_actions = _component_outcome(component, ranked, target_category)
        enriched_components.append({**component, "results": ranked, "outcome": outcome,
                                    "next_actions": next_actions})

    verified = reachable or not all_refs  # an all-write slate has nothing to verify and is not "unverified"
    results = {
        "verified": verified,
        "rate_limited": any_rate_limited,
        "target_license_category": target_category if target_license else None,
        "components": enriched_components,
    }
    if not verified and all_refs:
        results["reason"] = "no repository could be reached (offline, or every request failed)"

    results["table"] = _render_table(idea, enriched_components)
    report_id = save_report(idea, slate, results, owner=owner, project_id=project_id)
    return {**results, "id": report_id, "exit_code": 0}


def _render_table(idea: str, components: List[Dict[str, Any]]) -> str:
    lines = []
    if idea:
        lines.append(f"Prior art for: {idea}")
        lines.append("")
    lines.append("| component | verdict | repo | status | license | last push | ★ |")
    lines.append("|---|---|---|---|---|---|---|")
    for component in components:
        results = component.get("results") or []
        if not results:
            lines.append(f"| {component['name']} | {component['verdict']} | — | — | — | — | — |")
            continue
        for entry in results:
            repo = entry.get("full_name") or entry.get("requested", "?")
            if entry.get("renamed"):
                repo += f" (was {entry['requested']})"
            status = entry.get("status", "?")
            license_spdx = entry.get("license_spdx") or "—"
            pushed = (entry.get("pushed_at") or "—")[:10]
            stars = entry.get("stars", "—")
            lines.append(f"| {component['name']} | {component['verdict']} | {repo} | {status} | "
                        f"{license_spdx} | {pushed} | {stars} |")
        for action in component.get("next_actions") or []:
            lines.append(f"| | | *next: {action}* | | | | |")
    return "\n".join(lines)


# ── search ───────────────────────────────────────────────────────────────

def search(query: str, *, language: str = "", limit: int = 8, include_stale: bool = False) -> Dict[str, Any]:
    """GitHub repository search — for when the model has no candidate to
    verify. Results carry the same verified fields as `verify` (they come
    from the same API), so the model can pass any of them straight into a
    slate's `repos`."""
    query = (query or "").strip()
    limit = max(1, min(int(limit or 8), 25))
    if not query:
        return {"error": "prior_art search: query is required", "exit_code": 1}

    if not _enabled():
        return {"verified": False, "reason": "prior_art is disabled (setting prior_art_enabled=false)",
                "results": [], "exit_code": 0}

    q = query
    if language:
        q += f" language:{language}"
    if not include_stale:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%d")
        q += f" pushed:>{cutoff}"

    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_S, follow_redirects=True, headers=_headers()) as client:
            resp = client.get(f"{GITHUB_API}/search/repositories",
                              params={"q": q, "sort": "stars", "order": "desc", "per_page": limit})
    except httpx.RequestError as exc:
        return {"verified": False, "reason": f"network error: {exc}", "results": [], "exit_code": 0}

    remaining = resp.headers.get("X-RateLimit-Remaining")
    if resp.status_code == 403 and remaining == "0":
        return {"verified": False, "reason": "GitHub API rate limit exhausted",
                "rate_limited": True, "results": [],
                "rate_limit_reset": resp.headers.get("X-RateLimit-Reset"), "exit_code": 0}
    if resp.status_code != 200:
        return {"verified": False, "reason": f"GitHub API returned HTTP {resp.status_code}",
                "results": [], "exit_code": 0}

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        return {"verified": False, "reason": f"could not parse GitHub API response: {exc}",
                "results": [], "exit_code": 0}

    results = []
    for item in (data.get("items") or [])[:limit]:
        license_info = item.get("license") or {}
        results.append({
            "full_name": item.get("full_name"),
            "description": (item.get("description") or "")[:280],
            "stars": item.get("stargazers_count", 0),
            "open_issues": item.get("open_issues_count", 0),
            "pushed_at": item.get("pushed_at"),
            "archived": bool(item.get("archived")),
            "fork": bool(item.get("fork")),
            "default_branch": item.get("default_branch"),
            "homepage": item.get("homepage") or "",
            "html_url": item.get("html_url") or "",
            "license_spdx": license_info.get("spdx_id"),
            "license_category": _license_category(license_info.get("spdx_id")),
            "health": _health(item.get("pushed_at")),
        })
    return {"verified": True, "query": q, "total_count": data.get("total_count", len(results)),
            "results": results, "exit_code": 0}


__all__ = [
    "rubric", "verify", "search", "save_report", "report", "reports",
    "license_compatibility",
]
