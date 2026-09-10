"""Isolated browser sessions per owner and per task (WEB-03).

Today's single browser profile — ``builtin_mcp.py::_browser_profile_dir()``,
one directory under ``DATA_DIR/browser-profile`` shared by every owner and
every task, selected only by the global ``browser_profile`` setting
("persistent" | "isolated") — is exactly what
``docs/spec/v2/MAPA_REUTILIZACION.md`` (WEB-03, "ausente") describes: no
per-owner/per-task profile, no declared download/login policy, no
close-on-task-end. This module adds that layer *alongside* the existing
profile rather than replacing it (rule 3: no capability lost) — a caller
that never asks for a session keeps using the single shared profile exactly
as it does today.

``open_session`` hands back a :class:`BrowserSession` whose ``profile_dir``
is unique per ``(owner_id, task_id)`` — see :func:`_profile_dir_for` for why
that makes cross-owner cookie leakage a path collision that cannot happen
rather than a rule that has to be remembered. ``close_session`` removes only
that per-task directory: it can never reach the shared persistent profile,
which lives under a completely different path this module never touches
(the WEB-03 acceptance criterion "cerrar una tarea no borra la sesión del
navegador personal del usuario").

Wiring this into the live Playwright MCP launch (``src/builtin_mcp.py``) is
outside this lote's file ownership; see the final report for the exact
integration point.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from src.contracts.base import now_iso


@dataclass(frozen=True)
class BrowserSessionPolicy:
    """What a browser session is allowed to do, declared up front.

    Both default to ``False`` — "closed unless asked for" — so a session
    opened without an explicit policy cannot download files or reuse a saved
    login just because the underlying browser tooling supports it.
    """

    allow_downloads: bool = False
    allow_logins: bool = False


@dataclass(frozen=True)
class BrowserSession:
    owner_id: str
    task_id: str
    profile_dir: str
    policy: BrowserSessionPolicy
    ephemeral: bool
    opened_at: str
    closed_at: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


def _safe_component(value: str) -> str:
    """A filesystem-safe, collision-resistant path segment for `value`.

    Hashing rather than sanitizing the raw id sidesteps the usual path
    tricks (`../..`, embedded separators, case-folding collisions on some
    filesystems) without needing a separate validator: two different ids
    hash to two different directories, full stop.
    """
    return hashlib.sha256(str(value or "").encode("utf-8", "replace")).hexdigest()[:32]


def _sessions_root(base_dir: Optional[str] = None) -> str:
    if base_dir:
        return os.path.join(base_dir, "browser-sessions")
    try:
        from src.constants import DATA_DIR
        root = DATA_DIR
    except Exception:
        root = os.path.join(os.getcwd(), "data")
    return os.path.join(root, "browser-sessions")


def _profile_dir_for(owner_id: str, task_id: str, *, base_dir: Optional[str] = None) -> str:
    """Deterministic, owner-and-task-scoped profile directory.

    Deterministic so a second ``open_session`` call for the same
    ``(owner_id, task_id)`` (a retried tool call, a reconnect) lands on the
    same cookies instead of orphaning a fresh, empty profile every time.
    """
    root = _sessions_root(base_dir)
    return os.path.join(root, _safe_component(owner_id), _safe_component(task_id))


# In-memory registry of sessions this process has opened, keyed by
# (owner_id, task_id) — mirrors the ENGINE_HEALTH / lock pattern
# services/search/providers.py already uses for shared, mutable state.
_SESSIONS: Dict[Tuple[str, str], BrowserSession] = {}
_SESSIONS_LOCK = threading.Lock()


def open_session(
    owner_id: str,
    task_id: str,
    *,
    allow_downloads: bool = False,
    allow_logins: bool = False,
    ephemeral: bool = False,
    base_dir: Optional[str] = None,
) -> BrowserSession:
    """Open (or resume) the isolated browser session for `(owner_id, task_id)`.

    Reopening the same, still-open key is idempotent and keeps the original
    policy — a second tool call cannot quietly widen `allow_downloads` on a
    session another call already started. Reopening a CLOSED key starts a
    fresh session (new `opened_at`, policy taken from this call), matching
    WEB-03's "sesiones efímeras opcionales": a session's lifetime is the task's,
    not the process's.
    """
    owner = str(owner_id or "").strip() or "anonymous"
    task = str(task_id or "").strip() or "default"
    key = (owner, task)
    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(key)
        if existing is not None and existing.is_open:
            return existing
        profile_dir = _profile_dir_for(owner, task, base_dir=base_dir)
        session = BrowserSession(
            owner_id=owner,
            task_id=task,
            profile_dir=profile_dir,
            policy=BrowserSessionPolicy(allow_downloads=bool(allow_downloads), allow_logins=bool(allow_logins)),
            ephemeral=bool(ephemeral),
            opened_at=now_iso(),
        )
        if not session.ephemeral:
            try:
                os.makedirs(profile_dir, exist_ok=True)
            except OSError:
                pass
        _SESSIONS[key] = session
        return session


def get_session(owner_id: str, task_id: str) -> Optional[BrowserSession]:
    key = (str(owner_id or "").strip() or "anonymous", str(task_id or "").strip() or "default")
    with _SESSIONS_LOCK:
        return _SESSIONS.get(key)


def close_session(owner_id: str, task_id: str, *, delete_profile: bool = True) -> bool:
    """Close the session for `(owner_id, task_id)`; True if one was open.

    Only ever deletes the per-task directory `_profile_dir_for` computed for
    THIS key — never a parent directory, and never the single shared
    persistent profile `builtin_mcp._browser_profile_dir()` manages, which
    lives under a sibling path this module has no reference to. That is what
    makes the WEB-03 acceptance criterion ("closing a task does not delete
    the user's personal browser session") true by construction rather than
    by a check that could be skipped.
    """
    owner = str(owner_id or "").strip() or "anonymous"
    task = str(task_id or "").strip() or "default"
    key = (owner, task)
    with _SESSIONS_LOCK:
        existing = _SESSIONS.get(key)
        if existing is None or not existing.is_open:
            return False
        closed = replace(existing, closed_at=now_iso())
        _SESSIONS[key] = closed
    if delete_profile and not closed.ephemeral:
        try:
            shutil.rmtree(closed.profile_dir, ignore_errors=True)
        except Exception:
            pass
    return True


def policy_allows(session: BrowserSession, action: str) -> Tuple[bool, Optional[str]]:
    """(allowed, reason) for `action` under `session`'s declared policy.

    `action` is a coarse capability name ("download", "reuse_login") rather
    than a Playwright tool name, so a caller in `src/mcp_manager.py` (outside
    this lote's file ownership — see the final report) can gate
    `browser_file_upload`/download handling and stored-credential reuse
    without this module knowing the MCP tool vocabulary.
    """
    if not session.is_open:
        return False, f"browser session for task {session.task_id!r} is closed"
    if action == "download" and not session.policy.allow_downloads:
        return False, "downloads are not allowed for this browser session"
    if action == "reuse_login" and not session.policy.allow_logins:
        return False, "reusing a saved login is not allowed for this browser session"
    return True, None
