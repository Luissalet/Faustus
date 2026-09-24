"""src/code_history.py — git history understanding for the codebase explorer.

Answers, for a file or a symbol inside it: who changed it, how often, with
which commits, what else usually changes alongside it, and the risk that
implies. Everything here goes through the `git` binary via `subprocess`,
confined to the caller's workspace and bounded by a timeout; nothing raises
past this module's own boundary — a broken repo, a missing `git`, or a
symbol that no longer exists all come back as `{"error": ...}` instead of an
exception, exactly like `src.code_graph.query` and `src.git_panel`.

Public functions:
    file_history(workspace, path, *, limit=30)
    symbol_history(workspace, path, symbol, *, limit=20)
    blame_summary(workspace, path, *, start=None, end=None)
    co_change(workspace, path, *, limit=200)
    risk(workspace, path)
    explain(workspace, path, symbol=None) -> combines all of the above,
        plus a `summary_md` for a chat/UI answer.

A small in-memory cache keys on (workspace, HEAD sha, path, symbol) so a
follow-up question about the same file at the same commit is free; it holds
at most `_CACHE_MAX` entries (LRU eviction) and is naturally invalidated the
moment HEAD moves.
"""
from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
from collections import OrderedDict, Counter
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10
_CACHE_MAX = 200
_DIFF_EXCERPT_LINES = 60

_cache: "OrderedDict[tuple, Any]" = OrderedDict()


def _cache_get(key: tuple) -> Any:
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    return None


def _cache_put(key: tuple, value: Any) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_cache() -> None:
    """Test hook / manual reset."""
    _cache.clear()


# ---------------------------------------------------------------------------
# git subprocess helper — never raises
# ---------------------------------------------------------------------------
def _run_git(workspace: str, args: List[str], *, timeout: float = _TIMEOUT_S) -> Tuple[bool, str, str]:
    """(ok, stdout, stderr). ok is False on a non-zero exit, a timeout, a
    missing `git`, or any other subprocess failure — callers never see an
    exception."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", "git command timed out"
    except FileNotFoundError:
        return False, "", "git is not installed on this host"
    except Exception as exc:  # noqa: BLE001 - subprocess is inherently flaky
        return False, "", str(exc)
    return proc.returncode == 0, proc.stdout or "", proc.stderr or ""


def _is_git_repo(workspace: str) -> bool:
    ok, out, _err = _run_git(workspace, ["rev-parse", "--is-inside-work-tree"])
    return ok and out.strip() == "true"


def _head_sha(workspace: str) -> str:
    ok, out, _err = _run_git(workspace, ["rev-parse", "HEAD"])
    return out.strip() if ok else ""


def _rel_path(workspace: str, path: str) -> str:
    """`path` relative to `workspace`, forward slashes, for git's own paths."""
    raw = str(path or "").strip()
    if not raw:
        return raw
    if os.path.isabs(raw):
        try:
            raw = os.path.relpath(raw, workspace)
        except ValueError:
            pass
    return raw.replace(os.sep, "/")


# ---------------------------------------------------------------------------
# file_history
# ---------------------------------------------------------------------------
_LOG_SEP = "\x1f"
_REC_SEP = "\x1e"


def file_history(workspace: str, path: str, *, limit: int = 30) -> Dict[str, Any]:
    """Commits touching `path`, newest first: sha7, date, author, subject —
    plus per-commit churn (added/deleted lines via `--numstat`), first/last
    touched dates and an authors ranking."""
    try:
        if not _is_git_repo(workspace):
            return {"error": f"not a git repository: {workspace}"}
        rel = _rel_path(workspace, path)
        if not rel:
            return {"error": "path is required"}
        limit = max(1, min(int(limit or 30), 500))
        fmt = f"{_REC_SEP}%h{_LOG_SEP}%ad{_LOG_SEP}%an{_LOG_SEP}%s"
        ok, out, err = _run_git(
            workspace,
            ["log", f"-n{limit}", f"--pretty=format:{fmt}", "--date=short",
             "--numstat", "--", rel],
        )
        if not ok:
            return {"error": f"git log failed: {err.strip() or 'unknown error'}"}
        commits: List[Dict[str, Any]] = []
        for block in out.split(_REC_SEP):
            block = block.strip("\n")
            if not block:
                continue
            lines = block.split("\n")
            header = lines[0]
            parts = header.split(_LOG_SEP)
            if len(parts) < 4:
                continue
            sha, date, author, subject = parts[0], parts[1], parts[2], parts[3]
            adds = dels = 0
            for stat_line in lines[1:]:
                stat_line = stat_line.strip()
                if not stat_line:
                    continue
                cols = stat_line.split("\t")
                if len(cols) < 3:
                    continue
                a, d = cols[0], cols[1]
                try:
                    adds += int(a)
                except ValueError:
                    pass
                try:
                    dels += int(d)
                except ValueError:
                    pass
            commits.append({
                "sha": sha, "date": date, "author": author, "subject": subject,
                "adds": adds, "dels": dels,
            })
        authors = Counter(c["author"] for c in commits)
        return {
            "path": rel,
            "commits": commits,
            "commit_count": len(commits),
            "first_touched": commits[-1]["date"] if commits else None,
            "last_touched": commits[0]["date"] if commits else None,
            "authors": [{"name": name, "commits": n} for name, n in authors.most_common()],
            "total_adds": sum(c["adds"] for c in commits),
            "total_dels": sum(c["dels"] for c in commits),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_history.file_history failed: %s", exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# symbol_history
# ---------------------------------------------------------------------------
def _supports_dash_l(workspace: str) -> bool:
    ok, out, _err = _run_git(workspace, ["log", "-L", ":main:setup.py", "-n1"])
    # Even when the symbol/file lookup itself fails, git recognizes -L syntax
    # unless the whole feature is missing (very old git); we only need to
    # know the flag is understood, so any run that isn't "unknown option"
    # counts as supported.
    if ok:
        return True
    low = _err.lower()
    return "unknown option" not in low and "unrecognized" not in low


def _find_symbol_range(workspace: str, path: str, symbol: str) -> Optional[Tuple[int, int]]:
    """Best-effort (start, end) 1-based line range of `symbol`'s current
    definition — `ast` for Python, a regex for everything else."""
    abs_path = os.path.join(workspace, path) if not os.path.isabs(path) else path
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None

    if abs_path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name == symbol:
                        end = getattr(node, "end_lineno", None) or node.lineno
                        return node.lineno, end

    # Regex fallback (JS/TS/other): function foo(...), class Foo, const foo =
    lines = text.split("\n")
    pattern = re.compile(
        r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+" + re.escape(symbol) + r"\b"
        r"|class\s+" + re.escape(symbol) + r"\b"
        r"|(?:const|let|var)\s+" + re.escape(symbol) + r"\s*=)"
    )
    for i, line in enumerate(lines):
        if pattern.search(line):
            start = i + 1
            # crude end: next top-level (unindented, non-blank) line, or EOF
            end = len(lines)
            indent = len(line) - len(line.lstrip())
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if not nxt.strip():
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if nxt_indent <= indent and j > i:
                    end = j
                    break
            return start, end
    return None


def symbol_history(workspace: str, path: str, symbol: str, *, limit: int = 20) -> Dict[str, Any]:
    """Commits that touched `symbol`'s definition in `path`, newest first,
    plus a clipped diff excerpt (<=60 lines) for the most recent one. Uses
    `git log -L :<symbol>:<path>` when the host's git understands it;
    otherwise finds the symbol's current line range (ast for Python, regex
    otherwise) and uses `git log -L start,end:path`."""
    try:
        if not _is_git_repo(workspace):
            return {"error": f"not a git repository: {workspace}"}
        rel = _rel_path(workspace, path)
        if not rel or not symbol:
            return {"error": "path and symbol are required"}
        limit = max(1, min(int(limit or 20), 200))

        used_range: Optional[Tuple[int, int]] = None
        if _supports_dash_l(workspace):
            ok, out, err = _run_git(
                workspace, ["log", f"-n{limit}", f"-L:{symbol}:{rel}", "--date=short"],
                timeout=_TIMEOUT_S * 2,
            )
        else:
            ok, out, err = False, "", "git -L unsupported"

        if not ok or not out.strip():
            rng = _find_symbol_range(workspace, rel, symbol)
            if rng is None:
                return {"error": f"symbol {symbol!r} not found in {rel!r}"}
            used_range = rng
            ok, out, err = _run_git(
                workspace,
                ["log", f"-n{limit}", f"-L{rng[0]},{rng[1]}:{rel}", "--date=short"],
                timeout=_TIMEOUT_S * 2,
            )
            if not ok:
                return {"error": f"git log -L failed: {err.strip() or 'unknown error'}"}

        commits = _parse_log_L(out)
        excerpt = ""
        if commits:
            excerpt = commits[0].pop("_raw", "")
            excerpt = "\n".join(excerpt.split("\n")[:_DIFF_EXCERPT_LINES])
        for c in commits:
            c.pop("_raw", None)
        return {
            "path": rel, "symbol": symbol, "commits": commits,
            "commit_count": len(commits),
            "line_range": list(used_range) if used_range else None,
            "latest_diff_excerpt": excerpt,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_history.symbol_history failed: %s", exc)
        return {"error": str(exc)}


_LOG_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{7,40})")
_LOG_AUTHOR_RE = re.compile(r"^Author:\s*(.+)$")
_LOG_DATE_RE = re.compile(r"^Date:\s*(.+)$")


def _parse_log_L(out: str) -> List[Dict[str, Any]]:
    """Split `git log -L` output into one record per commit: sha, author,
    date, subject and the raw hunk text (kept in `_raw` for the caller to
    turn into the latest-commit excerpt, then dropped)."""
    commits: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    body_lines: List[str] = []
    subject_next = False
    for line in out.split("\n"):
        m = _LOG_COMMIT_RE.match(line)
        if m:
            if current is not None:
                current["_raw"] = "\n".join(body_lines)
                commits.append(current)
            current = {"sha": m.group(1)[:7], "author": "", "date": "", "subject": ""}
            body_lines = [line]
            subject_next = False
            continue
        if current is None:
            continue
        body_lines.append(line)
        am = _LOG_AUTHOR_RE.match(line)
        if am:
            current["author"] = am.group(1).strip()
            continue
        dm = _LOG_DATE_RE.match(line)
        if dm:
            current["date"] = dm.group(1).strip()
            subject_next = True
            continue
        if subject_next and line.strip():
            current["subject"] = line.strip()
            subject_next = False
    if current is not None:
        current["_raw"] = "\n".join(body_lines)
        commits.append(current)
    return commits


# ---------------------------------------------------------------------------
# blame_summary
# ---------------------------------------------------------------------------
def blame_summary(workspace: str, path: str, *, start: Optional[int] = None,
                   end: Optional[int] = None) -> Dict[str, Any]:
    """`git blame --line-porcelain`, aggregated: {author: lines %}, newest /
    oldest commit touching a surviving line, and the "hot" commits (those
    responsible for the most lines currently in the file)."""
    try:
        if not _is_git_repo(workspace):
            return {"error": f"not a git repository: {workspace}"}
        rel = _rel_path(workspace, path)
        if not rel:
            return {"error": "path is required"}
        args = ["blame", "--line-porcelain"]
        if start and end:
            args += ["-L", f"{int(start)},{int(end)}"]
        args += ["--", rel]
        ok, out, err = _run_git(workspace, args, timeout=_TIMEOUT_S * 2)
        if not ok:
            return {"error": f"git blame failed: {err.strip() or 'unknown error'}"}

        author_lines: Counter = Counter()
        commit_lines: Counter = Counter()
        commit_meta: Dict[str, Dict[str, str]] = {}
        current_sha = ""
        for line in out.split("\n"):
            csha = re.match(r"^([0-9a-f]{40})\s", line)
            if csha and len(line.split()) >= 2 and re.match(r"^[0-9a-f]{40} \d+", line):
                current_sha = csha.group(1)
                continue
            if line.startswith("author "):
                commit_meta.setdefault(current_sha, {})["author"] = line[len("author "):].strip()
            elif line.startswith("author-time "):
                commit_meta.setdefault(current_sha, {})["time"] = line[len("author-time "):].strip()
            elif line.startswith("\t"):
                author = commit_meta.get(current_sha, {}).get("author", "unknown")
                author_lines[author] += 1
                commit_lines[current_sha] += 1

        total = sum(author_lines.values()) or 1
        by_author = [
            {"author": a, "lines": n, "pct": round(100.0 * n / total, 1)}
            for a, n in author_lines.most_common()
        ]
        dated = [
            (sha, int(meta.get("time") or 0)) for sha, meta in commit_meta.items() if meta.get("time")
        ]
        dated.sort(key=lambda t: t[1])
        oldest = dated[0][0][:7] if dated else None
        newest = dated[-1][0][:7] if dated else None
        hot = [
            {"sha": sha[:7], "lines": n, "author": commit_meta.get(sha, {}).get("author", "unknown")}
            for sha, n in commit_lines.most_common(5)
        ]
        return {
            "path": rel, "total_lines": total, "by_author": by_author,
            "oldest_commit": oldest, "newest_commit": newest, "hot_commits": hot,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_history.blame_summary failed: %s", exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# co_change
# ---------------------------------------------------------------------------
def co_change(workspace: str, path: str, *, limit: int = 200) -> Dict[str, Any]:
    """Files that most often appear in the same commits as `path`, over its
    last `limit` commits: counts and a ratio (co-occurrences / commits
    touching `path`) — "changing this usually also touches …"."""
    try:
        if not _is_git_repo(workspace):
            return {"error": f"not a git repository: {workspace}"}
        rel = _rel_path(workspace, path)
        if not rel:
            return {"error": "path is required"}
        limit = max(1, min(int(limit or 200), 2000))
        ok, out, err = _run_git(
            workspace, ["log", f"-n{limit}", "--pretty=format:%H", "--", rel],
        )
        if not ok:
            return {"error": f"git log failed: {err.strip() or 'unknown error'}"}
        shas = [s for s in out.split("\n") if s.strip()]
        if not shas:
            return {"path": rel, "commit_count": 0, "co_changed": []}

        counter: Counter = Counter()
        for sha in shas:
            ok2, files_out, _err2 = _run_git(
                workspace, ["show", "--name-only", "--pretty=format:", sha],
            )
            if not ok2:
                continue
            for f in files_out.split("\n"):
                f = f.strip()
                if f and f != rel:
                    counter[f] += 1

        total = len(shas)
        co_changed = [
            {"path": f, "count": n, "ratio": round(n / total, 3)}
            for f, n in counter.most_common(50)
        ]
        return {"path": rel, "commit_count": total, "co_changed": co_changed}
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_history.co_change failed: %s", exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# risk
# ---------------------------------------------------------------------------
def _related_tests_present(workspace: str, path: str) -> bool:
    try:
        from src.project_tests import related_test_files
        return bool(related_test_files(workspace, [path]))
    except Exception:
        return False


def risk(workspace: str, path: str) -> Dict[str, Any]:
    """Heuristic 0-1 risk score from: churn in the last 90 days, number of
    distinct authors, co-change fan-out, and whether a related test file
    exists — plus a short human explanation."""
    try:
        if not _is_git_repo(workspace):
            return {"error": f"not a git repository: {workspace}"}
        rel = _rel_path(workspace, path)
        if not rel:
            return {"error": "path is required"}

        ok, out, err = _run_git(
            workspace, ["log", "--since=90.days", "--pretty=format:%h|%an", "--", rel],
        )
        recent_commits = [l for l in out.split("\n") if l.strip()] if ok else []
        recent_authors = {l.split("|", 1)[1] for l in recent_commits if "|" in l}

        fh = file_history(workspace, rel, limit=500)
        all_authors = fh.get("authors", []) if "error" not in fh else []

        co = co_change(workspace, rel, limit=200)
        fan_out = len([c for c in co.get("co_changed", []) if c.get("ratio", 0) >= 0.3]) \
            if "error" not in co else 0

        has_tests = _related_tests_present(workspace, rel)

        churn_score = min(len(recent_commits) / 15.0, 1.0)
        author_score = min(len(all_authors) / 6.0, 1.0)
        fanout_score = min(fan_out / 8.0, 1.0)
        test_score = 0.0 if has_tests else 1.0

        score = round(
            0.35 * churn_score + 0.2 * author_score + 0.2 * fanout_score + 0.25 * test_score, 3
        )
        score = max(0.0, min(score, 1.0))

        reasons = []
        reasons.append(f"{len(recent_commits)} commit(s) in the last 90 days")
        reasons.append(f"{len(all_authors)} author(s) overall ({len(recent_authors)} recently)")
        reasons.append(f"{fan_out} file(s) usually change alongside it")
        reasons.append("has related tests" if has_tests else "no related test file found")

        return {
            "path": rel, "score": score,
            "recent_commits_90d": len(recent_commits),
            "authors_overall": len(all_authors),
            "authors_recent_90d": len(recent_authors),
            "co_change_fan_out": fan_out,
            "has_related_tests": has_tests,
            "explanation": "; ".join(reasons),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_history.risk failed: %s", exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# explain — combines everything
# ---------------------------------------------------------------------------
def explain(workspace: str, path: str, symbol: Optional[str] = None) -> Dict[str, Any]:
    try:
        if not _is_git_repo(workspace):
            return {"error": f"not a git repository: {workspace}"}
        rel = _rel_path(workspace, path)
        if not rel:
            return {"error": "path is required"}

        head = _head_sha(workspace)
        cache_key = (workspace, head, rel, symbol or "")
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        fh = file_history(workspace, rel)
        bs = blame_summary(workspace, rel)
        cc = co_change(workspace, rel)
        rk = risk(workspace, rel)
        sh = symbol_history(workspace, rel, symbol) if symbol else None

        lines = [f"### History for `{rel}`" + (f" · `{symbol}`" if symbol else "")]
        if "error" not in fh:
            top_authors = ", ".join(f"{a['name']} ({a['commits']})" for a in fh.get("authors", [])[:3])
            lines.append(
                f"- {fh.get('commit_count', 0)} commit(s) tracked, "
                f"{fh.get('first_touched')} → {fh.get('last_touched')}. Top authors: {top_authors or 'n/a'}."
            )
        if "error" not in bs:
            top = ", ".join(f"{a['author']} {a['pct']}%" for a in bs.get("by_author", [])[:3])
            lines.append(f"- Current lines by author: {top or 'n/a'}.")
        if "error" not in cc:
            top_co = ", ".join(f"{c['path']} ({c['ratio']*100:.0f}%)" for c in cc.get("co_changed", [])[:5])
            lines.append(f"- Usually changes together with: {top_co or 'nothing consistently'}.")
        if "error" not in rk:
            lines.append(f"- Risk: **{rk.get('score')}** — {rk.get('explanation')}.")
        if sh is not None and "error" not in sh:
            lines.append(f"- Symbol `{symbol}`: {sh.get('commit_count', 0)} commit(s) touched its body.")

        result = {
            "path": rel, "symbol": symbol or None,
            "file_history": fh, "blame_summary": bs, "co_change": cc, "risk": rk,
            "symbol_history": sh, "summary_md": "\n".join(lines),
        }
        _cache_put(cache_key, result)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_history.explain failed: %s", exc)
        return {"error": str(exc)}
