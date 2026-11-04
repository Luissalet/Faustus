"""src/worker_caches.py — every isolated worker gets its own toolchain caches.

Sandboxed agents can still talk to each other through the infrastructure they
share. A package cache is the classic channel: worker A drops a poisoned wheel
or tarball in ``~/.cache/pip`` or the npm cache, worker B installs it later and
runs it. Separate worktrees do not help when both workers resolve packages
through the same cache directory.

So whenever Faustus runs something inside an isolated copy of a project (a
fan-out candidate, an alternative's own tests), the subprocess environment
points ``PIP_CACHE_DIR``, ``npm_config_cache``, ``UV_CACHE_DIR``, the Yarn,
pnpm, Poetry, Go and NuGet caches and ``TMP``/``TEMP``/``TMPDIR`` at a folder
of that worker's own. The folder lives NEXT TO the workspace
(``<parent>/_caches/<name>``), never inside it, so it never shows up in the
candidate's diff, and it goes away with the experiment folder.

Second half: while a worker is isolated, any tool call whose text names a
path inside a SIBLING workspace (another candidate, another worker's caches)
or inside the host's shared package caches is noted — logged and appended to
``<cache root>/cross_reads.jsonl`` — so the fan-out report can say "this
candidate looked at another one's directory". It never blocks: workspace
confinement is the permission layer's job; this is the audit trail.

Usage::

    with worker_caches.isolated(alt_path):
        await run_worker(...)          # every bash/native call in here
                                       # gets the worker's own caches

    env = worker_caches.isolated_env(alt_path, native_host_environment())
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import time
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: variable -> subfolder of the worker's cache root. One folder per tool, so
#: two tools that disagree about layout never share one.
CACHE_VARS: Dict[str, str] = {
    "PIP_CACHE_DIR": "pip",
    "npm_config_cache": "npm",
    "UV_CACHE_DIR": "uv",
    "YARN_CACHE_FOLDER": "yarn",
    "npm_config_store_dir": "pnpm-store",
    "POETRY_CACHE_DIR": "poetry",
    "GOCACHE": "go-build",
    "GOMODCACHE": "go-mod",
    "NUGET_PACKAGES": "nuget",
    # Caches that live under HOME by default. HOME itself stays where it is
    # (git, ssh and the toolchains read it); only the cache paths move.
    "GRADLE_USER_HOME": "gradle",
    "DENO_DIR": "deno",
    "BUN_INSTALL_CACHE_DIR": "bun",
    "COMPOSER_CACHE_DIR": "composer",
    "PIPENV_CACHE_DIR": "pipenv",
    "PRE_COMMIT_HOME": "pre-commit",
    "CCACHE_DIR": "ccache",
    "SCCACHE_DIR": "sccache",
    "CYPRESS_CACHE_FOLDER": "cypress",
    "ELECTRON_CACHE": "electron",
    "CARGO_HOME": "cargo",
    "TMP": "tmp",
    "TEMP": "tmp",
    "TMPDIR": "tmp",
}

#: Maven has no cache variable: its local repository is a ``-D`` option,
#: appended to MAVEN_OPTS (whatever the user already put there is kept).
MAVEN_SUB = "maven"
#: Every folder a worker's cache root can hold, for sizes and clearing.
CACHE_SUBS = tuple(dict.fromkeys([*CACHE_VARS.values(), MAVEN_SUB]))
#: Variables that are only set when it is safe: see `cargo_has_toolchain`.
CONDITIONAL_VARS = frozenset({"CARGO_HOME"})

CACHES_DIRNAME = "_caches"
CROSS_READS_FILE = "cross_reads.jsonl"
MAX_CROSS_READS_KEPT = 200

#: What a worker's cache root says about itself (kind, name, session ids):
#: written once, the first time a command of that worker needs its caches,
#: so the Agents screen and ``GET /api/agents/worker-caches`` can list them.
META_FILE = "worker.json"
#: Delegated workers (delegate_agents, the minions of an agent definition,
#: dispatch and swarm workers) share the project folder, so their own
#: caches live in Faustus' data folder, one folder per worker.
WORKERS_DIRNAME = "worker_caches"
#: Cache roots that live elsewhere (a fan-out candidate's ``_caches``),
#: one absolute path per line, so the listing finds them too.
EXTERNAL_INDEX = "external_roots.txt"

_active: contextvars.ContextVar = contextvars.ContextVar("worker_cache_workspace", default=None)
_allowed: contextvars.ContextVar = contextvars.ContextVar("worker_cache_allowed", default=())
#: (cache_root, meta) of a delegated worker that has no workspace of its own.
_own_root: contextvars.ContextVar = contextvars.ContextVar("worker_cache_own_root", default=None)
#: Fan-out cache roots currently inside `isolated(...)`, for the "active" flag.
_LIVE_ROOTS: Dict[str, int] = {}


def workers_base_dir() -> str:
    """``<data dir>/worker_caches``; read at call time so tests can move it."""
    override = os.environ.get("FAUSTUS_WORKER_CACHES_DIR")
    if override:
        return os.path.abspath(override)
    try:
        from src.constants import DATA_DIR
    except Exception:  # noqa: BLE001 - constants unavailable in a bare import
        DATA_DIR = os.path.join(os.path.expanduser("~"), ".faustus")
    return os.path.join(str(DATA_DIR), WORKERS_DIRNAME)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(str(path)))))


def _is_under(path: str, root: str) -> bool:
    """Both already `_norm`-ed. True for the root itself too."""
    if not root:
        return False
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def cache_root_for(workspace: str) -> str:
    """Where `workspace`'s private caches live: ``<parent>/_caches/<name>``."""
    ws = os.path.normpath(os.path.abspath(str(workspace)))
    parent, name = os.path.split(ws.rstrip("\\/")) if ws.rstrip("\\/") else (ws, "root")
    return os.path.join(parent, CACHES_DIRNAME, name or "root")


def cargo_has_toolchain(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the CARGO_HOME this environment would use also holds the
    installed binaries (``bin/cargo``, rustup proxies, ``cargo install``
    tools). That folder is a toolchain as much as a cache, so a worker keeps
    it: moving CARGO_HOME there would hide the user's cargo from the worker."""
    env = os.environ if env is None else env
    home = env.get("CARGO_HOME") or os.path.join(os.path.expanduser("~"), ".cargo")
    return any(os.path.isfile(os.path.join(home, "bin", name)) for name in ("cargo", "cargo.exe"))


def _with_maven_repo(opts: str, repo: str) -> str:
    kept = [tok for tok in str(opts or "").split() if not tok.startswith("-Dmaven.repo.local=")]
    return " ".join(kept + [f'-Dmaven.repo.local={repo}'])


def isolated_env(workspace: Optional[str], base: Optional[Mapping[str, str]] = None, *,
                 cache_root: Optional[str] = None,
                 meta: Optional[Mapping[str, object]] = None) -> Dict[str, str]:
    """A copy of `base` (default: ``os.environ``) whose package caches and temp
    folders all point inside `workspace`'s own cache root (or `cache_root`
    when given: a delegated worker's folder). The folders are created,
    because pip, npm and Python's ``tempfile`` fail on a missing one instead
    of creating it."""
    env: Dict[str, str] = dict(os.environ if base is None else base)
    root = cache_root or cache_root_for(str(workspace or ""))
    _write_meta(root, meta or ({"kind": "fanout", "name": os.path.basename(str(workspace).rstrip("\\/")),
                                "workspace": os.path.abspath(str(workspace))} if workspace else {}))
    skip = {"CARGO_HOME"} if cargo_has_toolchain(env) else set()
    if os.name == "nt":
        # Windows variable names are case-insensitive: an inherited `Tmp` or
        # `NPM_CONFIG_CACHE` would otherwise sit next to ours in the block
        # and which one wins would be up to the child.
        wanted = {name.upper() for name in CACHE_VARS if name not in skip}
        wanted.add("MAVEN_OPTS")
        maven_opts = next((v for k, v in env.items() if k.upper() == "MAVEN_OPTS"), "")
        env = {k: v for k, v in env.items() if k.upper() not in wanted}
        if maven_opts:
            env["MAVEN_OPTS"] = maven_opts
    for name, sub in CACHE_VARS.items():
        if name in skip:
            continue
        path = os.path.join(root, sub)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:  # a read-only parent must not break the run
            logger.warning("[worker-caches] could not create %s: %s", path, exc)
        env[name] = path
    maven = os.path.join(root, MAVEN_SUB)
    try:
        os.makedirs(maven, exist_ok=True)
    except OSError as exc:
        logger.warning("[worker-caches] could not create %s: %s", maven, exc)
    env["MAVEN_OPTS"] = _with_maven_repo(env.get("MAVEN_OPTS", ""), maven)
    return env


@contextlib.contextmanager
def isolated(workspace: str, *, allow: Iterable[str] = ()) -> Iterator[str]:
    """Everything run inside this block (same task, or tasks it starts) gets
    `workspace`'s own caches. `allow` lists sibling paths that are fine to
    read (a frozen base snapshot, say). Yields the cache root."""
    token = _active.set(os.path.abspath(str(workspace)))
    allowed_token = _allowed.set(tuple(_norm(p) for p in allow if p))
    root = cache_root_for(workspace)
    key = _norm(root)
    _LIVE_ROOTS[key] = _LIVE_ROOTS.get(key, 0) + 1
    try:
        yield root
    finally:
        left = _LIVE_ROOTS.get(key, 1) - 1
        if left > 0:
            _LIVE_ROOTS[key] = left
        else:
            _LIVE_ROOTS.pop(key, None)
        _allowed.reset(allowed_token)
        _active.reset(token)


def active_workspace() -> Optional[str]:
    """The isolated worker workspace of the current context, if any."""
    return _active.get()


def worker_cache_root(worker_id: str) -> str:
    """A delegated worker's own cache folder: ``<data>/worker_caches/<id>``."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(worker_id or "worker")).strip("._") or "worker"
    return os.path.join(workers_base_dir(), safe[:80])


def enter_worker(worker_id: str, **meta: object) -> str:
    """Give the CURRENT context (one delegated worker's task) its own caches.

    Called at the start of a delegated worker's coroutine; the context
    variable dies with that task, so no reset is needed. A worker that is
    already inside a fan-out candidate (``isolated(...)``) keeps the
    candidate's caches, which sit next to its own folder; the return value
    is whichever root the worker's commands will use. Nothing is created on
    disk until a command needs the folders."""
    ws = _active.get()
    if ws:
        return cache_root_for(ws)
    root = worker_cache_root(worker_id)
    info = {"kind": "delegate", "worker_id": str(worker_id), "created": time.time()}
    info.update({k: v for k, v in meta.items() if v not in (None, "")})
    _own_root.set((root, info))
    return root


def active_cache_root() -> Optional[str]:
    """The cache root the current context's commands get, if any."""
    ws = _active.get()
    if ws:
        return cache_root_for(ws)
    own = _own_root.get()
    return own[0] if own else None


def apply_active(env: Mapping[str, str]) -> Dict[str, str]:
    """`env` with the current worker's caches, or `env` unchanged (as a new
    dict) when nothing is isolated."""
    ws = _active.get()
    if ws:
        return isolated_env(ws, env)
    own = _own_root.get()
    if own:
        return isolated_env(None, env, cache_root=own[0], meta=own[1])
    return dict(env)


# ---------------------------------------------------------------------------
# Cross reads: a worker naming another worker's folder or the shared caches
# ---------------------------------------------------------------------------
_PATH_RE = re.compile(r"(?:(?<![\w])[A-Za-z]:[\\/]|~[\\/]|\.\.[\\/]|(?<![\w.])/)[^\s\"'`|;&<>()]*")


def shared_cache_dirs(env: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
    """The host's own package caches: what the variables point at in the
    server's environment, plus each tool's default location."""
    env = os.environ if env is None else env
    home = os.path.expanduser("~")
    local = env.get("LOCALAPPDATA") or ""
    found: List[str] = []
    for name, sub in CACHE_VARS.items():
        if sub == "tmp" or name in CONDITIONAL_VARS:
            continue  # the host temp folder is everybody's; too noisy to flag
        value = env.get(name) or env.get(name.upper())
        if value:
            found.append(value)
    found += [
        os.path.join(home, ".npm"), os.path.join(home, ".cache", "pip"),
        os.path.join(home, ".cache", "uv"), os.path.join(home, ".cache", "yarn"),
        os.path.join(home, ".cache", "pypoetry"), os.path.join(home, ".nuget", "packages"),
    ]
    if local:
        found += [
            os.path.join(local, "npm-cache"), os.path.join(local, "pip", "Cache"),
            os.path.join(local, "uv", "cache"), os.path.join(local, "Yarn", "Cache"),
            os.path.join(local, "pypoetry", "Cache"),
        ]
    return tuple(dict.fromkeys(_norm(p) for p in found if p))


_QUOTED_RE = re.compile(r'"([^"\r\n]{2,})"|\'([^\'\r\n]{2,})\'')
_PATHLIKE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|~[\\/]|\.\.[\\/]|/)")


def _json_strings(value: object, out: List[str], depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _json_strings(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _json_strings(v, out, depth + 1)


def _candidates(text: str) -> List[str]:
    """Every path-like string a call names: whole JSON string values (tool
    arguments, escapes decoded), whole quoted strings (spaces kept: a folder
    such as `Proyectos independientes` is one path, not two), and the bare
    unquoted paths the regex finds."""
    found: List[str] = []
    stripped = text.strip()
    if stripped[:1] in "{[":
        try:
            values: List[str] = []
            _json_strings(json.loads(stripped), values)
        except ValueError:
            values = []
        else:
            # Tool arguments: each string value on its own, escapes decoded.
            for v in values:
                v = v.strip()
                if _PATHLIKE_RE.match(v):
                    found.append(v)
                elif v and v != stripped:
                    found.extend(_candidates(v))
            return found
    spans: List[Tuple[int, int]] = []
    for m in _QUOTED_RE.finditer(text):
        q = (m.group(1) or m.group(2) or "").strip()
        if _PATHLIKE_RE.match(q):
            found.append(q)
            spans.append(m.span())
    for m in _PATH_RE.finditer(text):
        # A bare match inside a quoted path is a truncated piece of it
        # (it stops at the first space); the whole quoted path is above.
        if any(a <= m.start() < b for a, b in spans):
            continue
        found.append(m.group(0))
    return found


def foreign_paths(text: str, workspace: str, *, allow: Iterable[str] = (),
                  shared: Optional[Iterable[str]] = None) -> List[Dict[str, str]]:
    """Absolute paths in `text` that leave `workspace` for a sibling
    workspace (``kind="sibling"``) or a shared package cache
    (``kind="shared_cache"``). The worker's own folder and own caches, and
    anything under `allow`, are fine; any other path is not this module's
    business."""
    if not text or not workspace:
        return []
    ws = _norm(workspace)
    parent = os.path.dirname(ws)
    if os.path.dirname(parent) == parent:
        parent = ""  # a workspace at a drive root has no siblings to speak of
    own_cache = _norm(cache_root_for(workspace))
    allowed = tuple(_norm(p) for p in allow if p)
    caches = tuple(_norm(c) for c in shared) if shared is not None else shared_cache_dirs()
    hits: List[Dict[str, str]] = []
    seen = set()
    for raw in _candidates(str(text)):
        raw = raw.rstrip(".,:")
        if raw.startswith(".."):
            # Commands run with the worker's folder as cwd, so `../b/x`
            # is a sibling's file.
            raw = os.path.join(workspace, raw)
        elif len(raw) < 3:
            continue
        path = _norm(raw)
        if path in seen or _is_under(path, ws) or _is_under(path, own_cache):
            continue
        if any(_is_under(path, a) for a in allowed):
            continue
        if parent and _is_under(path, parent) and path != parent:
            kind = "sibling"
        elif any(_is_under(path, c) for c in caches):
            kind = "shared_cache"
        else:
            continue
        seen.add(path)
        hits.append({"path": path, "kind": kind})
    return hits


def note_cross_reads(text: str, tool: str = "", *, workspace: Optional[str] = None) -> List[Dict[str, str]]:
    """Record the foreign paths a call by the isolated worker names.
    No-op (empty list) outside `isolated(...)`. Never raises."""
    ws = workspace or _active.get()
    if not ws:
        return []
    try:
        hits = foreign_paths(text, ws, allow=_allowed.get() if workspace is None else ())
        if not hits:
            return []
        root = cache_root_for(ws)
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, CROSS_READS_FILE)
        now = time.time()
        with open(path, "a", encoding="utf-8") as fh:
            for hit in hits:
                fh.write(json.dumps({"ts": now, "tool": str(tool or ""), **hit}, ensure_ascii=False) + "\n")
        logger.warning("[worker-caches] %s in %s named %d foreign path(s): %s",
                       tool or "a call", ws, len(hits), ", ".join(h["path"] for h in hits[:3]))
        return hits
    except Exception as exc:  # noqa: BLE001 - an audit note must never break a tool call
        logger.debug("[worker-caches] could not note cross reads: %s", exc)
        return []


def read_cross_reads(workspace: str, *, limit: int = MAX_CROSS_READS_KEPT) -> List[Dict[str, object]]:
    """What `note_cross_reads` recorded for `workspace`, oldest first."""
    path = os.path.join(cache_root_for(workspace), CROSS_READS_FILE)
    out: List[Dict[str, object]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out[-max(1, int(limit)):]


# ---------------------------------------------------------------------------
# Listing and clearing: what the Agents screen, REST and MCP show
# ---------------------------------------------------------------------------
#: Stop counting a folder after this many files; the size is then a floor.
MAX_FILES_COUNTED = 50000


def _write_meta(root: str, meta: Mapping[str, object]) -> None:
    """Write ``worker.json`` once per cache root, and remember a root that
    lives outside the workers folder. Never raises."""
    if not meta:
        return
    path = os.path.join(root, META_FILE)
    if os.path.exists(path):
        return
    try:
        os.makedirs(root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(dict(meta, root=os.path.abspath(root)), fh, ensure_ascii=False)
        base = workers_base_dir()
        if not _is_under(_norm(root), _norm(base)):
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, EXTERNAL_INDEX), "a", encoding="utf-8") as fh:
                fh.write(os.path.abspath(root) + "\n")
    except OSError as exc:
        logger.debug("[worker-caches] could not write %s: %s", path, exc)


def _known_roots() -> List[str]:
    """Every cache root with a ``worker.json``: the delegated workers' folders
    plus the fan-out roots recorded in the external index."""
    base = workers_base_dir()
    roots: List[str] = []
    try:
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isfile(os.path.join(path, META_FILE)):
                roots.append(path)
    except OSError:
        pass
    try:
        with open(os.path.join(base, EXTERNAL_INDEX), "r", encoding="utf-8") as fh:
            for line in fh:
                path = line.strip()
                if path and os.path.isfile(os.path.join(path, META_FILE)):
                    roots.append(path)
    except OSError:
        pass
    seen, out = set(), []
    for root in roots:
        key = _norm(root)
        if key not in seen:
            seen.add(key)
            out.append(root)
    return out


def _folder_size(path: str, budget: List[int]) -> Tuple[int, int]:
    """(bytes, files) under `path`; `budget` is the files left to count."""
    total = files = 0
    for dirpath, _dirs, names in os.walk(path):
        for name in names:
            if budget[0] <= 0:
                return total, files
            budget[0] -= 1
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                continue
    return total, files


def _live_worker_ids() -> set:
    try:
        from src.agent_tools.subagent_tools import active_worker_ids
        return set(active_worker_ids())
    except Exception:  # noqa: BLE001 - no delegation module, nobody is live
        return set()


def _read_meta(root: str) -> Dict[str, object]:
    try:
        with open(os.path.join(root, META_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def describe(root: str, *, live: Optional[set] = None, budget: Optional[List[int]] = None) -> Dict[str, object]:
    """One cache root: who it belongs to, whether that worker is running,
    and the size of each tool's folder."""
    meta = _read_meta(root)
    live = _live_worker_ids() if live is None else live
    budget = budget if budget is not None else [MAX_FILES_COUNTED]
    tools: Dict[str, int] = {}
    total = files = 0
    for sub in CACHE_SUBS:
        size, count = _folder_size(os.path.join(root, sub), budget)
        tools[sub] = size
        total += size
        files += count
    sid = str(meta.get("session_id") or meta.get("worker_id") or "")
    active = _norm(root) in _LIVE_ROOTS or (bool(sid) and sid in live)
    return {
        "root": os.path.abspath(root),
        "kind": str(meta.get("kind") or "fanout"),
        "name": str(meta.get("name") or os.path.basename(root)),
        "session_id": sid,
        "parent_session_id": str(meta.get("parent_session_id") or ""),
        "agent": str(meta.get("agent") or ""),
        "role": str(meta.get("role") or ""),
        "workspace": str(meta.get("workspace") or ""),
        "created": meta.get("created") or None,
        "active": bool(active),
        "bytes": total,
        "files": files,
        "tools": tools,
        "approximate": budget[0] <= 0,
        "cross_reads": len(read_cross_reads_root(root)),
    }


def read_cross_reads_root(root: str) -> List[Dict[str, object]]:
    try:
        with open(os.path.join(root, CROSS_READS_FILE), "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()][-MAX_CROSS_READS_KEPT:]
    except (OSError, ValueError):
        return []


def list_caches(*, session_id: str = "", parent_session_id: str = "", kind: str = "") -> Dict[str, object]:
    """Every worker's own cache folder, newest first, with sizes."""
    live = _live_worker_ids()
    budget = [MAX_FILES_COUNTED]
    rows = [describe(root, live=live, budget=budget) for root in _known_roots()]
    if session_id:
        rows = [r for r in rows if r["session_id"] == session_id]
    if parent_session_id:
        rows = [r for r in rows if r["parent_session_id"] == parent_session_id]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    rows.sort(key=lambda r: float(r.get("created") or 0), reverse=True)
    return {
        "base": workers_base_dir(),
        "variables": sorted(CACHE_VARS),
        "workers": rows,
        "total_bytes": sum(int(r["bytes"]) for r in rows),
        "active": sum(1 for r in rows if r["active"]),
    }


class WorkerCacheError(ValueError):
    """A clear request that names no known cache root, or a running one."""


def clear(*, root: str = "", session_id: str = "", all_idle: bool = False,
          force: bool = False, forget: bool = False) -> Dict[str, object]:
    """Empty the tool folders of one worker's caches (by `root` or
    `session_id`), or of every idle worker with `all_idle`. Only roots that
    `list_caches` knows are touched: an arbitrary path is refused. A running
    worker is skipped unless `force`. `forget` removes the folder and its
    ``worker.json`` as well, so the worker drops off the list."""
    import shutil

    known = {_norm(r): r for r in _known_roots()}
    live = _live_worker_ids()
    if all_idle:
        targets = list(known.values())
    elif root:
        if _norm(root) not in known:
            raise WorkerCacheError(f"not a worker cache folder Faustus knows: {root}")
        targets = [known[_norm(root)]]
    elif session_id:
        targets = [r for r in known.values()
                   if str(_read_meta(r).get("session_id") or _read_meta(r).get("worker_id") or "") == session_id]
        if not targets:
            raise WorkerCacheError(f"no cache folder for worker {session_id}")
    else:
        raise WorkerCacheError("say which worker: root, session_id or all")
    cleared: List[Dict[str, object]] = []
    skipped: List[Dict[str, object]] = []
    freed = 0
    for target in targets:
        row = describe(target, live=live)
        if row["active"] and not force:
            skipped.append({"root": row["root"], "reason": "the worker is running"})
            continue
        if forget:
            shutil.rmtree(target, ignore_errors=True)
        else:
            for sub in CACHE_SUBS:
                path = os.path.join(target, sub)
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
        # A file another process holds open (Windows) survives rmtree: count
        # what is really gone, not what was there.
        left = int(describe(target, live=live)["bytes"]) if os.path.isdir(target) else 0
        gone = max(0, int(row["bytes"]) - left)
        freed += gone
        entry = {"root": row["root"], "name": row["name"], "freed_bytes": gone,
                 "forgotten": bool(forget) and not left}
        if left:
            entry["remaining_bytes"] = left
        cleared.append(entry)
    return {"cleared": cleared, "skipped": skipped, "freed_bytes": freed}
