"""Safe mode — OPS-05 / QA-46.

Two different problems share one answer here:

* An **operator** knows something is wrong and wants to start with the
  extras off: `FAUSTUS_SAFE_MODE=1` in the environment.
* The **app itself** notices something is wrong — two boots in a row that
  never finished — and does the same thing without being asked, because the
  person who needs safe mode most is the one who does not know the phrase
  for it yet.

Either way "safe mode" means the same, narrow thing: external MCP servers,
third-party plugins/skills, and scheduled tasks are held back; chat, the
file/document tools and settings — the core a person needs to actually fix
whatever is wrong — keep working. Nothing here invents a second place to
record "this is disabled": server-level quarantine reuses `disabled_tools`,
the exact setting `src.mcp_manager.builtin_browser_policy_disabled` and the
tool-call gate already read, so a quarantined server is invisible to the
rest of the app through the path that already existed rather than a new one
next to it.

State survives a restart by living in `src.settings` (the app's one settings
store) under keys namespaced `_safe_mode_*` — not a parallel JSON file.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: A connection in either of these mcp_manager states (see `src/mcp_manager.py`,
#: `self._connections[server_id]["status"]`) counts as a boot problem — no new
#: state is invented, these are the two the manager already reports.
FAILING_STATUSES = ("error", "needs_auth")

#: Consecutive same-cause failures (per server, across restarts) before that
#: server is quarantined automatically rather than just retried.
MCP_QUARANTINE_THRESHOLD = 3

#: Consecutive boots that never reached `mark_boot_completed()` before safe
#: mode engages on its own, no env var required.
BOOT_FAILURE_THRESHOLD = 2

_ENV_VAR = "FAUSTUS_SAFE_MODE"

#: Settings keys. All prefixed so a settings dump or a "reset to defaults"
#: pass can recognize and skip this app-managed bookkeeping at a glance.
_K_BOOT_IN_PROGRESS = "_safe_mode_boot_in_progress"
_K_BOOT_FAILURES = "_safe_mode_boot_failures"
_K_ACTIVE = "_safe_mode_active"
_K_REASON = "_safe_mode_reason"
_K_ACTIVATED_AT = "_safe_mode_activated_at"
_K_REACTIVATED = "_safe_mode_reactivated"       # list[str] subsystems turned back on this session
_K_MCP_FAILS = "_safe_mode_mcp_fail_counts"      # dict[server_id, int]
_K_QUARANTINED = "_safe_mode_quarantined"        # list[str] server_ids safe_mode itself disabled

#: Everything OFF while safe mode is active and not individually reactivated.
#: Core — chat, the file/document tools, settings — is never in this dict:
#: it has no on/off switch here because safe mode never touches it.
SUBSYSTEMS = ("mcp_external", "plugins_third_party", "skills_third_party", "scheduled_tasks")

_SUBSYSTEM_LABEL = {
    "mcp_external": "External MCP servers (built-in tools still work)",
    "plugins_third_party": "Third-party plugins",
    "skills_third_party": "Skills not written by the owner",
    "scheduled_tasks": "Scheduled / recurring tasks",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get(key: str, default: Any) -> Any:
    from src.settings import get_setting
    return get_setting(key, default)


def _set(patch: Dict[str, Any]) -> None:
    """Persist into the SAME settings document `src.settings` already owns.

    Not `update_settings()`: that API validates every key against
    `DEFAULT_SETTINGS`, a schema this module does not own and should not
    have to extend for its own bookkeeping keys. `save_settings()` is the
    store's own documented "legacy whole-document write" path — still the
    one settings file, same locks, same revision counter — and `_merge()`
    keeps any key present in the saved document even when it is not in
    `DEFAULT_SETTINGS`, so `_safe_mode_*` keys read back through the
    ordinary `get_setting()` exactly like a declared one would.
    """
    from src.settings import load_settings, save_settings
    try:
        current = load_settings()
        current.update(patch)
        save_settings(current)
    except Exception as e:
        # Safe mode degrading itself over a settings-write hiccup would be the
        # one time it matters most; log and keep going with in-memory state.
        logger.warning("[safe_mode] could not persist %s: %s", list(patch), e)


# ── env / boot-failure detection ────────────────────────────────────────────

def env_forced() -> bool:
    """True when the operator asked for safe mode explicitly."""
    return os.getenv(_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def mark_boot_started() -> int:
    """Call once, early in application startup, before anything risky runs.

    Returns the number of PRIOR boots that started and never finished — the
    signal for "the app itself crashed last time(s)", independent of whether
    an operator ever set the env var. A boot that finishes clears this via
    `mark_boot_completed()`; one that does not leaves it standing, so the
    NEXT start sees it.
    """
    from src.settings import get_setting
    still_in_progress = bool(get_setting(_K_BOOT_IN_PROGRESS, False))
    failures = int(get_setting(_K_BOOT_FAILURES, 0) or 0)
    if still_in_progress:
        failures += 1
    _set({_K_BOOT_IN_PROGRESS: True, _K_BOOT_FAILURES: failures})
    return failures


def mark_boot_completed() -> None:
    """Call once startup has actually finished — the app is serving."""
    _set({_K_BOOT_IN_PROGRESS: False, _K_BOOT_FAILURES: 0})


def consecutive_boot_failures() -> int:
    return int(_get(_K_BOOT_FAILURES, 0) or 0)


def should_activate() -> "tuple[bool, str]":
    """Whether THIS boot should come up in safe mode, and why."""
    if env_forced():
        return True, f"{_ENV_VAR}=1 was set"
    failures = consecutive_boot_failures()
    if failures >= BOOT_FAILURE_THRESHOLD:
        return True, f"{failures} consecutive boots did not finish starting"
    return False, ""


# ── activation ───────────────────────────────────────────────────────────────

_active_cache: Optional[bool] = None
_reason_cache: str = ""


def activate(reason: str) -> Dict[str, Any]:
    """Turn safe mode on for this process, and record why."""
    global _active_cache, _reason_cache
    _active_cache, _reason_cache = True, reason
    _set({_K_ACTIVE: True, _K_REASON: reason, _K_ACTIVATED_AT: _now_iso(), _K_REACTIVATED: []})
    logger.warning("[safe_mode] ACTIVE: %s — %s", reason,
                   ", ".join(_SUBSYSTEM_LABEL[s] for s in SUBSYSTEMS))
    return status()


def deactivate() -> None:
    """Leave safe mode (an explicit operator action, not automatic)."""
    global _active_cache, _reason_cache
    _active_cache, _reason_cache = False, ""
    _set({_K_ACTIVE: False, _K_REASON: "", _K_REACTIVATED: []})


def is_active() -> bool:
    global _active_cache
    if _active_cache is not None:
        return _active_cache
    _active_cache = bool(_get(_K_ACTIVE, False))
    return _active_cache


def reason() -> str:
    global _reason_cache
    if _active_cache is None:
        is_active()
    if not _reason_cache:
        _reason_cache = str(_get(_K_REASON, "") or "")
    return _reason_cache


def reactivated() -> List[str]:
    return [str(s) for s in (_get(_K_REACTIVATED, []) or [])]


def reactivate_subsystem(name: str) -> Dict[str, Any]:
    """Turn ONE subsystem back on, one at a time — QA-46's "revision"."""
    if name not in SUBSYSTEMS:
        raise ValueError(f"unknown subsystem {name!r}; one of {SUBSYSTEMS}")
    current = reactivated()
    if name not in current:
        current.append(name)
        _set({_K_REACTIVATED: current})
    return status()


def disabled_subsystems() -> Dict[str, bool]:
    """{subsystem: still_disabled}. Empty (nothing disabled) when safe mode
    is not active — this is the single source both the API and the Studio
    panel read, so they cannot drift apart."""
    if not is_active():
        return {s: False for s in SUBSYSTEMS}
    on_again = set(reactivated())
    return {s: (s not in on_again) for s in SUBSYSTEMS}


# ── MCP quarantine — reuses mcp_manager's own needs_auth/error states ──────

def _mcp_fail_counts() -> Dict[str, int]:
    raw = _get(_K_MCP_FAILS, {}) or {}
    return {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def quarantined_servers() -> List[str]:
    return [str(s) for s in (_get(_K_QUARANTINED, []) or [])]


def is_quarantined(server_id: str) -> bool:
    return server_id in quarantined_servers()


def _add_to_disabled_tools(server_id: str) -> None:
    """Reuse `disabled_tools` — the SAME setting `mcp_manager`'s tool gate and
    `builtin_browser_policy_disabled()` already read — instead of a second,
    parallel disable list for the same idea."""
    from src.settings import get_setting
    current = get_setting("disabled_tools", []) or []
    names = [str(n) for n in current] if isinstance(current, (list, tuple)) else []
    if server_id not in names:
        names.append(server_id)
    _set({"disabled_tools": names})


def _remove_from_disabled_tools(server_id: str) -> None:
    from src.settings import get_setting
    current = get_setting("disabled_tools", []) or []
    names = [str(n) for n in current if str(n) != server_id] if isinstance(current, (list, tuple)) else []
    _set({"disabled_tools": names})


def record_mcp_connection(server_id: str, status: str) -> Dict[str, Any]:
    """Tell safe_mode about one connection attempt's outcome.

    `status` is whatever `mcp_manager` already put in
    `self._connections[server_id]["status"]`. A status in `FAILING_STATUSES`
    (today: `error`, `needs_auth`) counts against the server; anything else
    (`connected`, …) resets its count — a server that comes back healthy is
    not still "repeatedly crashing".

    At `MCP_QUARANTINE_THRESHOLD` consecutive failing attempts the server is
    quarantined: added to `disabled_tools` (so the existing tool gate keeps
    it out of the run) and to `_safe_mode_quarantined` (so the UI can say
    THIS ONE was disabled automatically, and by what, distinct from a
    server the operator turned off by hand).
    """
    counts = _mcp_fail_counts()
    if status in FAILING_STATUSES:
        counts[server_id] = counts.get(server_id, 0) + 1
    else:
        counts.pop(server_id, None)
    _set({_K_MCP_FAILS: counts})

    quarantined = quarantined_servers()
    newly_quarantined = False
    if counts.get(server_id, 0) >= MCP_QUARANTINE_THRESHOLD and server_id not in quarantined:
        quarantined.append(server_id)
        _set({_K_QUARANTINED: quarantined})
        _add_to_disabled_tools(server_id)
        newly_quarantined = True
        logger.warning(
            "[safe_mode] quarantined MCP server %r after %d consecutive %s — "
            "core stays up; reactivate it from Diagnostics once fixed",
            server_id, counts.get(server_id, 0), status)
    return {"server_id": server_id, "status": status, "fail_count": counts.get(server_id, 0),
            "quarantined": server_id in quarantined, "newly_quarantined": newly_quarantined}


def reactivate_mcp_server(server_id: str) -> Dict[str, Any]:
    """Un-quarantine one server, explicitly, one at a time."""
    quarantined = [s for s in quarantined_servers() if s != server_id]
    _set({_K_QUARANTINED: quarantined})
    counts = _mcp_fail_counts()
    counts.pop(server_id, None)
    _set({_K_MCP_FAILS: counts})
    _remove_from_disabled_tools(server_id)
    return {"server_id": server_id, "quarantined": False}


# ── status ───────────────────────────────────────────────────────────────

def status() -> Dict[str, Any]:
    return {
        "active": is_active(),
        "reason": reason(),
        "env_forced": env_forced(),
        "consecutive_boot_failures": consecutive_boot_failures(),
        "disabled": disabled_subsystems(),
        "reactivated": reactivated(),
        "quarantined_mcp_servers": quarantined_servers(),
        "mcp_fail_counts": _mcp_fail_counts(),
        "core_available": True,  # never gated by safe mode — chat, files, settings
    }


def run_startup_check() -> Dict[str, Any]:
    """The one call a startup hook needs: decide, activate if warranted, and
    return the status to log/report. Idempotent — safe to call more than once
    in a boot sequence."""
    if is_active():
        return status()
    should, why = should_activate()
    if should:
        return activate(why)
    return status()
