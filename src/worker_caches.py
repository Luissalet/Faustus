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
    "TMP": "tmp",
    "TEMP": "tmp",
    "TMPDIR": "tmp",
}

CACHES_DIRNAME = "_caches"
CROSS_READS_FILE = "cross_reads.jsonl"
MAX_CROSS_READS_KEPT = 200

_active: contextvars.ContextVar = contextvars.ContextVar("worker_cache_workspace", default=None)
_allowed: contextvars.ContextVar = contextvars.ContextVar("worker_cache_allowed", default=())


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


def isolated_env(workspace: str, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """A copy of `base` (default: ``os.environ``) whose package caches and temp
    folders all point inside `workspace`'s own cache root. The folders are
    created, because pip, npm and Python's ``tempfile`` fail on a missing one
    instead of creating it."""
    env: Dict[str, str] = dict(os.environ if base is None else base)
    root = cache_root_for(workspace)
    if os.name == "nt":
        # Windows variable names are case-insensitive: an inherited `Tmp` or
        # `NPM_CONFIG_CACHE` would otherwise sit next to ours in the block
        # and which one wins would be up to the child.
        wanted = {name.upper() for name in CACHE_VARS}
        env = {k: v for k, v in env.items() if k.upper() not in wanted}
    for name, sub in CACHE_VARS.items():
        path = os.path.join(root, sub)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:  # a read-only parent must not break the run
            logger.warning("[worker-caches] could not create %s: %s", path, exc)
        env[name] = path
    return env


@contextlib.contextmanager
def isolated(workspace: str, *, allow: Iterable[str] = ()) -> Iterator[str]:
    """Everything run inside this block (same task, or tasks it starts) gets
    `workspace`'s own caches. `allow` lists sibling paths that are fine to
    read (a frozen base snapshot, say). Yields the cache root."""
    token = _active.set(os.path.abspath(str(workspace)))
    allowed_token = _allowed.set(tuple(_norm(p) for p in allow if p))
    try:
        yield cache_root_for(workspace)
    finally:
        _allowed.reset(allowed_token)
        _active.reset(token)


def active_workspace() -> Optional[str]:
    """The isolated worker workspace of the current context, if any."""
    return _active.get()


def apply_active(env: Mapping[str, str]) -> Dict[str, str]:
    """`env` with the current worker's caches, or `env` unchanged (as a new
    dict) when nothing is isolated."""
    ws = _active.get()
    return isolated_env(ws, env) if ws else dict(env)


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
        if sub == "tmp":
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
    for match in _PATH_RE.finditer(str(text)):
        raw = match.group(0).rstrip(".,:")
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
