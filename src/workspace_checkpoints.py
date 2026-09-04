"""workspace_checkpoints.py — shadow snapshots of a workspace before the agent changes it.

A *shadow* git repository (git-dir under DATA_DIR/checkpoints/<id>/, work-tree =
the workspace) records the state of the workspace right before the first
mutation of a turn. It never touches the user's own ``.git`` (the user's repo
may not even exist), so it gives every agent turn:

  * a "restore to before this turn" baseline that works in any folder;
  * a per-file diff of what the turn changed, independent of the user's git;
  * the pre-edit content of a file (review mode: accept / reject per file).

Design notes
------------
* One shadow repo per workspace, keyed by the realpath. Checkpoints are commits
  on a single linear ref (``refs/heads/checkpoints``). Committing an unchanged
  tree reuses the previous commit, so idle turns cost one ``git add -A``.
* ``info/exclude`` of the shadow repo skips vendored/build directories and
  files above ``max_file_mb``; the workspace's own ``.gitignore`` files are
  honoured by git itself.
* Every git call is bounded by a timeout and wrapped: a checkpoint failure must
  never break a chat turn — callers get ``None`` and carry on.
* Per-workspace lock: two turns on the same folder (two chats, a coordinator and
  its workers) must not race on the shadow index.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from src.git_invariants import check_preconditions
from src.native_env import native_host_environment

logger = logging.getLogger(__name__)

CHECKPOINT_REF = "refs/heads/checkpoints"
_GIT_TIMEOUT = 60.0
_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_EXCLUDE_CACHE: Dict[str, float] = {}      # shadow dir → last exclude refresh
_EXCLUDE_TTL_S = 120.0

# Directories that are never worth snapshotting (huge, regenerable).
EXCLUDED_DIRS = (
    ".git", "node_modules", "venv", ".venv", "env", ".env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".cache",
    "dist", "build", ".next", ".nuxt", ".turbo", "target", ".gradle",
    ".idea", ".vscode", "coverage", "htmlcov", "site-packages", ".parcel-cache",
    ".dart_tool", ".pub-cache", "Pods", "DerivedData", ".terraform", ".serverless",
    "ollama-models", ".ollama",
)
EXCLUDED_GLOBS = (
    "*.pyc", "*.pyo", "*.class", "*.o", "*.obj", "*.so", "*.dll", "*.dylib",
    "*.exe", "*.log", "*.sqlite", "*.sqlite3", "*.db", "*.db-journal",
    "*.gguf", "*.safetensors", "*.bin", "*.pt", "*.pth", "*.ckpt", "*.onnx",
    "*.zip", "*.tar", "*.gz", "*.7z", "*.rar", "*.iso", "*.dmg",
    "*.mp4", "*.mkv", "*.mov", "*.avi", "*.mp3", "*.wav", "*.flac",
    "*.psd", "*.blend", "*.blend1",
)


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return DATA_DIR
    except Exception:  # pragma: no cover - import fallback
        return os.path.join(os.getcwd(), "data")


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def _lock_for(root: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(root)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[root] = lock
        return lock


def _norm_root(workspace: str) -> str:
    return os.path.realpath(os.path.expanduser(workspace))


def shadow_dir(workspace: str) -> str:
    """Where the shadow git-dir of `workspace` lives (deterministic per path)."""
    root = _norm_root(workspace)
    key = root.replace("\\", "/")
    if os.name == "nt":
        key = key.lower()
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(_data_dir(), "checkpoints", digest)


def _git_env() -> Dict[str, str]:
    # No git call in this module wants our virtualenv, and `restore()` runs
    # `git checkout`, which fires the user's post-checkout hook when they have
    # set core.hooksPath globally — their hook, run against our interpreter.
    env = native_host_environment()
    env.update({
        "GIT_AUTHOR_NAME": "Faustus checkpoints",
        "GIT_AUTHOR_EMAIL": "odysseus@localhost",
        "GIT_COMMITTER_NAME": "Faustus checkpoints",
        "GIT_COMMITTER_EMAIL": "odysseus@localhost",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C.UTF-8",
    })
    # The user's global hooks/templates must not run inside the shadow repo.
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    return env


def _run(root: str, args: List[str], *, timeout: float = _GIT_TIMEOUT, binary: bool = False,
         check: bool = False) -> Optional[subprocess.CompletedProcess]:
    """Run git against the shadow repo of `root`. Returns None on OS/timeout errors."""
    gd = shadow_dir(root)
    cmd = ["git", "--git-dir", gd, "--work-tree", root, *args]
    try:
        if binary:
            proc = subprocess.run(cmd, cwd=root, capture_output=True, timeout=timeout, env=_git_env())
        else:
            proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, env=_git_env())
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("[checkpoint] git %s failed: %s", args[:2], e)
        return None
    if check and proc.returncode != 0:
        err = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b"").decode("utf-8", "replace")
        logger.debug("[checkpoint] git %s rc=%s: %s", " ".join(args[:3]), proc.returncode, err.strip()[:300])
    return proc


def git_available() -> bool:
    return shutil.which("git") is not None


def _write_exclude(root: str, gd: str, max_file_mb: float) -> None:
    """Refresh info/exclude: vendored dirs, binary globs and oversized files."""
    now = time.time()
    if now - _EXCLUDE_CACHE.get(gd, 0.0) < _EXCLUDE_TTL_S:
        return
    lines = ["# generated by Faustus — edit ODYSSEUS settings, not this file"]
    lines += [f"{d}/" for d in EXCLUDED_DIRS]
    lines += list(EXCLUDED_GLOBS)
    limit = max(0.5, float(max_file_mb or 8)) * 1024 * 1024
    big: List[str] = []
    excluded = set(EXCLUDED_DIRS)
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in excluded]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(p) > limit:
                        rel = os.path.relpath(p, root).replace(os.sep, "/")
                        big.append("/" + _escape_exclude(rel))
                except OSError:
                    continue
                if len(big) >= 2000:
                    raise StopIteration
    except StopIteration:
        pass
    except OSError as e:
        logger.debug("[checkpoint] size scan failed for %s: %s", root, e)
    if big:
        lines.append("# files above the checkpoint size limit")
        lines += big
    try:
        os.makedirs(os.path.join(gd, "info"), exist_ok=True)
        with open(os.path.join(gd, "info", "exclude"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        _EXCLUDE_CACHE[gd] = now
    except OSError as e:
        logger.debug("[checkpoint] exclude write failed: %s", e)


def _escape_exclude(rel: str) -> str:
    out = []
    for ch in rel:
        if ch in "[]*?\\#!":
            out.append("\\" + ch)
        elif ch == " ":
            out.append("\\ ")
        else:
            out.append(ch)
    return "".join(out)


def _ensure_repo(root: str) -> bool:
    gd = shadow_dir(root)
    if not os.path.isdir(os.path.join(gd, "objects")):
        try:
            os.makedirs(gd, exist_ok=True)
        except OSError as e:
            logger.warning("[checkpoint] cannot create %s: %s", gd, e)
            return False
        proc = _run(root, ["init", "-q"], check=True)
        if proc is None or proc.returncode != 0:
            return False
        for k, v in (
            ("core.autocrlf", "false"), ("core.safecrlf", "false"), ("core.longpaths", "true"),
            ("core.fsmonitor", "false"), ("core.untrackedCache", "true"), ("commit.gpgsign", "false"),
            ("gc.auto", "2000"), ("core.quotepath", "false"),
        ):
            _run(root, ["config", k, v])
        try:
            with open(os.path.join(gd, "description"), "w", encoding="utf-8") as f:
                f.write(f"Faustus checkpoints for {root}\n")
            with open(os.path.join(gd, "ODYSSEUS_WORKSPACE"), "w", encoding="utf-8") as f:
                f.write(root + "\n")
        except OSError:
            pass
    _write_exclude(root, gd, float(_setting("agent_checkpoint_max_file_mb", 8) or 8))
    return True


def _head(root: str) -> Optional[str]:
    proc = _run(root, ["rev-parse", "--verify", "-q", CHECKPOINT_REF])
    if proc and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def _dir_size_mb(path: str) -> float:
    total = 0
    try:
        for dirpath, _dirs, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return total / (1024 * 1024)


def enabled() -> bool:
    return bool(_setting("agent_checkpoints", True)) and git_available()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def checkpoint(workspace: str, label: str = "") -> Optional[Dict[str, Any]]:
    """Snapshot the workspace now. Returns {"sha", "created", "reused", "ts", "label"}
    or None when checkpoints are unavailable (no git, unwritable data dir…)."""
    if not workspace or not git_available():
        return None
    root = _norm_root(workspace)
    if not os.path.isdir(root):
        return None
    with _lock_for(root):
        t0 = time.time()
        gd = shadow_dir(root)
        # Bound disk use: a shadow repo that outgrew the limit is reset. Old
        # checkpoints are lost, the current turn still gets its baseline.
        try:
            cap = float(_setting("agent_checkpoint_max_repo_mb", 2048) or 0)
        except (TypeError, ValueError):
            cap = 2048.0
        if cap and os.path.isdir(gd) and _dir_size_mb(gd) > cap:
            logger.warning("[checkpoint] shadow repo for %s exceeds %s MB — resetting", root, cap)
            # _reset_locked, NOT reset(): we already hold _lock_for(root) and
            # it is a plain, non-reentrant threading.Lock — calling the public
            # reset() here deadlocked the workspace forever.
            _reset_locked(root)
        if not _ensure_repo(root):
            return None
        add = _run(root, ["add", "-A", "--ignore-errors", "--", "."], timeout=_GIT_TIMEOUT * 3, check=True)
        if add is None:
            return None
        tree = _run(root, ["write-tree"], check=True)
        if tree is None or tree.returncode != 0:
            return None
        tree_sha = tree.stdout.strip()
        head = _head(root)
        if head:
            head_tree = _run(root, ["rev-parse", f"{head}^{{tree}}"])
            if head_tree and head_tree.stdout.strip() == tree_sha:
                return {"sha": head, "tree": tree_sha, "created": False, "reused": True,
                        "ts": time.time(), "label": label, "ms": int((time.time() - t0) * 1000)}
        msg = label or f"checkpoint {time.strftime('%Y-%m-%d %H:%M:%S')}"
        args = ["commit-tree", tree_sha, "-m", msg]
        if head:
            args += ["-p", head]
        commit = _run(root, args, check=True)
        if commit is None or commit.returncode != 0:
            return None
        sha = commit.stdout.strip()
        upd = _run(root, ["update-ref", CHECKPOINT_REF, sha] + ([head] if head else []), check=True)
        if upd is None or upd.returncode != 0:
            return None
        logger.info("[checkpoint] %s → %s (%d ms)", root, sha[:10], int((time.time() - t0) * 1000))
        return {"sha": sha, "tree": tree_sha, "created": True, "reused": False,
                "ts": time.time(), "label": label, "ms": int((time.time() - t0) * 1000)}


def _refresh_index(root: str) -> bool:
    add = _run(root, ["add", "-A", "--ignore-errors", "--", "."], timeout=_GIT_TIMEOUT * 3)
    return bool(add is not None and add.returncode == 0)


def changed_since(workspace: str, sha: str, paths: Optional[Iterable[str]] = None) -> List[Dict[str, str]]:
    """Files whose current content differs from checkpoint `sha`:
    [{"status": "M"|"A"|"D", "path": "rel/path"}]. Empty on any failure."""
    if not workspace or not sha or not git_available():
        return []
    root = _norm_root(workspace)
    with _lock_for(root):
        if not _ensure_repo(root) or not _refresh_index(root):
            return []
        args = ["diff", "--cached", "--name-status", "--no-renames", "-z", sha]
        rel_paths = [_rel(root, p) for p in (paths or []) if p]
        rel_paths = [p for p in rel_paths if p]
        if paths is not None and not rel_paths:
            return []
        if rel_paths:
            args += ["--", *rel_paths]
        proc = _run(root, args, check=True)
    if proc is None or proc.returncode != 0:
        return []
    out: List[Dict[str, str]] = []
    parts = proc.stdout.split("\0")
    i = 0
    while i + 1 < len(parts):
        status, path = parts[i], parts[i + 1]
        i += 2
        if not status or not path:
            continue
        out.append({"status": status[:1], "path": path})
    return out


def diff_since(workspace: str, sha: str, path: Optional[str] = None, max_chars: int = 400_000) -> str:
    """Unified diff of the work tree against checkpoint `sha` (new files included)."""
    if not workspace or not sha or not git_available():
        return ""
    root = _norm_root(workspace)
    with _lock_for(root):
        if not _ensure_repo(root) or not _refresh_index(root):
            return ""
        args = ["diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames", sha]
        if path:
            rel = _rel(root, path)
            if not rel:
                return ""
            args += ["--", rel]
        proc = _run(root, args, timeout=_GIT_TIMEOUT * 2, check=True)
    if proc is None or proc.returncode != 0:
        return ""
    text = proc.stdout or ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… diff truncated"
    return text


def file_at(workspace: str, sha: str, path: str) -> Optional[bytes]:
    """Content of `path` at checkpoint `sha`, or None if it did not exist."""
    if not workspace or not sha or not path or not git_available():
        return None
    root = _norm_root(workspace)
    rel = _rel(root, path)
    if not rel:
        return None
    proc = _run(root, ["show", f"{sha}:{rel}"], binary=True)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def has_checkpoint(workspace: str, sha: str) -> bool:
    """Does this workspace's shadow repo actually know that checkpoint?

    Worth its own function because every read here — `diff_since`,
    `changed_since`, `file_at` — answers a sha it has never heard of with the
    same empty result it gives for "nothing changed". Those are opposite
    facts, and a caller that cannot tell them apart will report "no changes"
    about a checkpoint from a different machine, a different data directory,
    or one that a `reset()` threw away."""
    if not workspace or not sha or not git_available():
        return False
    root = _norm_root(workspace)
    proc = _run(root, ["cat-file", "-e", f"{sha}^{{commit}}"])
    return bool(proc is not None and proc.returncode == 0)


def exists_at(workspace: str, sha: str, path: str) -> bool:
    if not workspace or not sha or not path or not git_available():
        return False
    root = _norm_root(workspace)
    rel = _rel(root, path)
    if not rel:
        return False
    proc = _run(root, ["cat-file", "-e", f"{sha}:{rel}"])
    return bool(proc is not None and proc.returncode == 0)


def restore(workspace: str, sha: str, paths: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Put the listed paths (default: every path that differs) back to their
    state at checkpoint `sha`. Files created after the checkpoint are deleted.
    Returns {"restored": [...], "deleted": [...], "failed": [...], "unchanged": n}."""
    result: Dict[str, Any] = {"restored": [], "deleted": [], "failed": [], "unchanged": 0, "sha": sha}
    if not workspace or not sha or not git_available():
        result["error"] = "checkpoints unavailable"
        return result
    root = _norm_root(workspace)
    if paths is not None:
        wanted = [p for p in (_rel(root, p) for p in paths) if p]
        if not wanted:
            result["error"] = "no valid paths"
            return result
    else:
        wanted = None
    changed = changed_since(root, sha, wanted)
    if wanted is not None:
        changed_map = {c["path"]: c for c in changed}
        result["unchanged"] = len([p for p in wanted if p not in changed_map])
        targets = [changed_map[p] for p in wanted if p in changed_map]
    else:
        targets = changed
    with _lock_for(root):
        for c in targets:
            rel, status = c["path"], c["status"]
            abs_path = os.path.join(root, *rel.split("/"))
            try:
                if status == "A":
                    # Created after the checkpoint → remove it.
                    if os.path.isfile(abs_path) or os.path.islink(abs_path):
                        os.remove(abs_path)
                    result["deleted"].append(rel)
                    continue
                proc = _run(root, ["checkout", "-q", sha, "--", rel], check=True)
                if proc is None or proc.returncode != 0:
                    result["failed"].append(rel)
                else:
                    result["restored"].append(rel)
            except OSError as e:
                logger.debug("[checkpoint] restore %s failed: %s", rel, e)
                result["failed"].append(rel)
    # Keep the shadow index in step with the tree we just wrote.
    try:
        from src import agent_harness as _h
        _h.invalidate_index(root)
    except Exception:
        pass
    return result


def export_tree(workspace: str, sha: str, dest: str) -> bool:
    """Materialise checkpoint `sha` under `dest` (a fresh directory): the
    tracked files as they were, nothing else. Used to run the project's tests
    against the pre-turn state ("did this fail before my change?")."""
    if not workspace or not sha or not dest or not git_available():
        return False
    import zipfile
    root = _norm_root(workspace)
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        return False
    zip_path = os.path.join(dest, "__checkpoint__.zip")
    proc = _run(root, ["archive", "--format=zip", "-o", zip_path, sha], timeout=_GIT_TIMEOUT * 3, check=True)
    if proc is None or proc.returncode != 0 or not os.path.isfile(zip_path):
        return False
    try:
        with zipfile.ZipFile(zip_path) as z:
            for member in z.infolist():
                name = member.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    continue
                z.extract(member, dest)
    except (OSError, zipfile.BadZipFile) as e:
        logger.debug("[checkpoint] export of %s failed: %s", sha[:10], e)
        return False
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    return True


def list_checkpoints(workspace: str, limit: int = 30) -> List[Dict[str, Any]]:
    if not workspace or not git_available():
        return []
    root = _norm_root(workspace)
    if not os.path.isdir(os.path.join(shadow_dir(root), "objects")):
        return []
    proc = _run(root, ["log", CHECKPOINT_REF, f"-n{max(1, int(limit))}", "--format=%H%x1f%ct%x1f%s"])
    if proc is None or proc.returncode != 0:
        return []
    out: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        bits = line.split("\x1f")
        if len(bits) >= 3:
            try:
                ts = int(bits[1])
            except ValueError:
                ts = 0
            out.append({"sha": bits[0], "ts": ts, "label": bits[2]})
    return out


def _reset_locked(root: str) -> bool:
    """Body of `reset()` WITHOUT the per-workspace lock.

    `root` must already be normalised and the caller must already hold
    `_lock_for(root)` (or be sure nobody else can). Callers that run inside a
    locked section use this; everything else uses `reset()`.
    """
    gd = shadow_dir(root)
    _EXCLUDE_CACHE.pop(gd, None)
    if not os.path.isdir(gd):
        return True
    try:
        _rmtree_force(gd)
        return True
    except OSError as e:
        logger.warning("[checkpoint] reset of %s failed: %s", gd, e)
        return False


def reset(workspace: str) -> bool:
    """Delete the shadow repo of a workspace (frees disk; loses old baselines)."""
    if not workspace:
        return False
    root = _norm_root(workspace)
    with _lock_for(root):
        return _reset_locked(root)


def _rmtree_force(path: str) -> None:
    """rmtree that copes with git's read-only object files (Windows refuses to
    delete them with a plain rmtree: WinError 5)."""
    import stat

    def _clear_and_retry(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            raise
    try:
        shutil.rmtree(path, onexc=lambda f, p, e: _clear_and_retry(f, p, e))     # Python ≥ 3.12
    except TypeError:
        shutil.rmtree(path, onerror=lambda f, p, ei: _clear_and_retry(f, p, ei[1]))


def status(workspace: str) -> Dict[str, Any]:
    root = _norm_root(workspace) if workspace else ""
    gd = shadow_dir(root) if root else ""
    present = bool(gd and os.path.isdir(os.path.join(gd, "objects")))
    return {
        "enabled": enabled(),
        "git": git_available(),
        "workspace": root,
        "shadow_dir": gd,
        "present": present,
        "size_mb": round(_dir_size_mb(gd), 1) if present else 0.0,
        "head": _head(root) if present else None,
        "count": len(list_checkpoints(root, 500)) if present else 0,
    }


def _rel(root: str, path: str) -> Optional[str]:
    """Path relative to the workspace root (forward slashes) or None when it
    escapes the root."""
    if not path:
        return None
    try:
        candidate = path if os.path.isabs(path) else os.path.join(root, path)
        real = os.path.realpath(candidate)
    except (OSError, ValueError):
        return None
    root_cmp, real_cmp = root, real
    if os.name == "nt":
        root_cmp, real_cmp = root.lower(), real.lower()
    if real_cmp != root_cmp and not real_cmp.startswith(root_cmp.rstrip(os.sep) + os.sep):
        return None
    rel = os.path.relpath(real, root).replace(os.sep, "/")
    if rel == ".":
        return None
    return rel


# ---------------------------------------------------------------------------
# The user's own repository: "commit these changes"
# ---------------------------------------------------------------------------

def user_repo_root(workspace: str) -> Optional[str]:
    """Top-level of the USER's git repo containing the workspace, or None."""
    if not workspace or not git_available():
        return None
    root = _norm_root(workspace)
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=10,
                              env=native_host_environment(extra={"GIT_TERMINAL_PROMPT": "0"}))
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return os.path.realpath(proc.stdout.strip())


def user_git_commit(workspace: str, paths: Iterable[str], message: str) -> Dict[str, Any]:
    """`git add -- <paths>` + `git commit -m <message> -- <paths>` in the user's
    repo. Only the given paths are committed, whatever else is staged."""
    top = user_repo_root(workspace)
    if not top:
        return {"ok": False, "error": "not a git repository"}
    # Committing into a repository that is mid-rebase or on a detached HEAD is
    # destructive and leaves no trace the person would recognise as a failure,
    # so it is refused and reported rather than attempted. There is deliberately
    # no way to override this.
    pre = check_preconditions(top)
    if not pre.ok:
        for problem in pre.problems:
            logger.warning("[checkpoint] refusing to commit in %s: %s", top, problem)
        return {"ok": False, "error": " ".join(pre.problems)[:400],
                "problems": list(pre.problems), "refused": True}
    root = _norm_root(workspace)
    rels: List[str] = []
    for p in paths:
        abs_p = p if os.path.isabs(p) else os.path.join(root, p)
        try:
            real = os.path.realpath(abs_p)
        except (OSError, ValueError):
            continue
        top_cmp, real_cmp = (top.lower(), real.lower()) if os.name == "nt" else (top, real)
        if not real_cmp.startswith(top_cmp.rstrip(os.sep) + os.sep):
            continue
        rels.append(os.path.relpath(real, top).replace(os.sep, "/"))
    if not rels:
        return {"ok": False, "error": "no files inside the repository"}
    message = (message or "").strip() or "Changes made by the Faustus agent"
    # `git commit` runs the repository's pre-commit hook — the user's own code,
    # often python. With our virtualenv inherited it resolves against our
    # interpreter and our site-packages instead of their project's.
    env = native_host_environment(extra={"GIT_TERMINAL_PROMPT": "0"})
    try:
        add = subprocess.run(["git", "add", "-A", "--", *rels], cwd=top, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30, env=env)
        if add.returncode != 0:
            return {"ok": False, "error": (add.stderr or "git add failed")[:400]}
        commit = subprocess.run(["git", "commit", "-q", "-m", message, "--", *rels], cwd=top,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": str(e)[:400]}
    if commit.returncode != 0:
        err = (commit.stderr or commit.stdout or "git commit failed").strip()
        if "nothing to commit" in err or "no changes added" in err:
            return {"ok": False, "error": "nothing to commit for those files", "nothing": True}
        if "Please tell me who you are" in err or "user.email" in err:
            return {"ok": False, "error": "git identity not configured (git config user.name / user.email)"}
        return {"ok": False, "error": err[:400]}
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=top, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10, env=env).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return {"ok": True, "sha": sha, "files": rels, "message": message, "repo": top}


_MSG_STRIP_RE = re.compile(r"^(?:please|por favor|puedes|podr[ií]as|can you|could you|hey|hola|oye)[\s,:]+", re.I)


def propose_commit_message(user_text: str, files: Iterable[str], language: str = "en") -> str:
    """A reasonable default commit message from the request + the changed files.
    The user edits it before committing; it only needs to be sensible."""
    text = " ".join((user_text or "").split())
    text = _MSG_STRIP_RE.sub("", text).strip()
    text = re.sub(r"^[/@#!]\S*\s*", "", text)      # slash commands, mentions
    subject = text.split(". ")[0].strip(" .:;,-")
    if not subject:
        subject = "Cambios del agente" if language == "es" else "Agent changes"
    if len(subject) > 72:
        subject = subject[:69].rstrip() + "…"
    subject = subject[0].upper() + subject[1:]
    file_list = [str(f) for f in files if f]
    body_lines: List[str] = []
    if file_list:
        shown = file_list[:12]
        body_lines.append(("Archivos: " if language == "es" else "Files: ") + ", ".join(shown)
                          + (f" (+{len(file_list) - len(shown)})" if len(file_list) > len(shown) else ""))
    body_lines.append("Made with the Faustus agent.")
    return subject + "\n\n" + "\n".join(body_lines)


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
