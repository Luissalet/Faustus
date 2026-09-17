"""src/launch_profiles.py — F1.4: user-defined "start this local app" profiles.

Everything in this module assumes the CRUD/launch boundary the contract
draws in one sentence: *"El modelo NO obtiene ejecución arbitraria: las
herramientas del agente no pueden crear ni editar perfiles ni pasar argv."*
This module itself has no notion of "who is calling" — that boundary is
enforced entirely by `routes/connector_routes.py` gating every mutating and
launching route behind `require_human` (which explicitly refuses the
in-process agent-tool token — see `core/middleware.py::require_human`)
instead of `require_admin` (which accepts it, same as every other MCP admin
route). A profile is validated the same way at save time and at launch time:
absolute paths, real files/directories, a plain list of argv strings, no
shell involved anywhere (`subprocess.Popen(..., shell=False)`, always).

Idempotency (F1.4): a launch that finds the app already answering its
readiness URL, or its own previously-spawned pid still alive, returns
``{"launched": False, "already_running": True}`` instead of spawning a
second process. Concurrent double-clicks are serialised by a per-profile
lock so only one of them ever gets past that check.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from core.atomic_io import atomic_write_json
from core.platform_compat import IS_WINDOWS, safe_chmod
from src.constants import BASE_DIR, DATA_DIR as _DEFAULT_DATA_DIR
from src.process_launch import LaunchResult, spawn_detached

logger = logging.getLogger(__name__)

# Module-level, like src/mcp_manager.py's own `DATA_DIR` — tests point this at
# a disposable tmp_path (`monkeypatch.setattr(launch_profiles, "DATA_DIR", ...)`)
# instead of the real data dir. Paths below are derived fresh on every call.
DATA_DIR = _DEFAULT_DATA_DIR


def _profiles_file() -> str:
    return os.path.join(DATA_DIR, "launch_profiles.json")


def _log_dir() -> str:
    return os.path.join(DATA_DIR, "launch_logs")

KINDS = ("process", "open_url", "open_exe")

_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

#: F1.4 "Apps": icon file rules for the new `icon` field — an absolute path
#: to a small image, checked both at save time and again at serve time (the
#: icon route refuses anything outside these extensions even if a profile
#: was hand-edited on disk).
ICON_CONTENT_TYPES: Dict[str, str] = {
    ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}
ICON_MAX_BYTES = 4 * 1024 * 1024
_OPEN_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
DESCRIPTION_MAX_CHARS = 500

_STORE_LOCK = threading.Lock()
#: One lock per profile_id, created on first use. Guarded by `_locks_guard`
#: only for the dict access itself, never held while the per-profile lock is.
_locks_guard = threading.Lock()
_profile_locks: Dict[str, threading.Lock] = {}

#: In-process record of a profile this Faustus process itself started:
#: profile_id -> {"pid", "spawned_at", "log_path"}. The cross-restart
#: idempotency signal is still the readiness URL first (the domain app is
#: still listening whether or not Faustus remembers spawning it) — but the
#: "Apps" wave also needs `status()`/`stop()` to recognise, right after a
#: Faustus restart, that a pid it owns is the SAME process it spawned before
#: (not a recycled one), so this dict is now mirrored to
#: `data/launch_state.json` (see `_load_own_launches`/`_persist_own_launches`)
#: and loaded lazily, pruning any pid that is no longer alive at that same
#: creation time.
_own_launches: Dict[str, Dict[str, Any]] = {}
#: DATA_DIR the above dict was last loaded/reconciled against — reload
#: (comparing, not blindly trusting, disk state) whenever DATA_DIR changes,
#: which is how tests get a fresh, isolated view per `tmp_path`.
_own_launches_loaded_for: Optional[str] = None


class ProfileValidationError(ValueError):
    pass


# ── storage ────────────────────────────────────────────────────────────────

def _load() -> Dict[str, Any]:
    path = _profiles_file()
    if not os.path.exists(path):
        return {"version": 1, "profiles": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load launch profiles: %s", exc)
        return {"version": 1, "profiles": []}
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), list):
        return {"version": 1, "profiles": []}
    return raw


def _save(data: Dict[str, Any]) -> None:
    path = _profiles_file()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_write_json(path, data, indent=2)
    safe_chmod(path, 0o600)


def _launch_state_path() -> str:
    return os.path.join(DATA_DIR, "launch_state.json")


def _load_own_launches() -> Dict[str, Dict[str, Any]]:
    """`_own_launches`, reconciled against `data/launch_state.json` once per
    DATA_DIR. A restarted Faustus starts this dict empty in memory, so the
    first call after a restart repopulates it from disk — pruning any entry
    whose pid is no longer alive at the creation time it was spawned with
    (`_pid_alive`), so a recycled pid never reads back as "still ours"."""
    global _own_launches_loaded_for
    if _own_launches_loaded_for != DATA_DIR:
        _own_launches_loaded_for = DATA_DIR
        _own_launches.clear()
        path = _launch_state_path()
        raw: Any = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to load launch_state.json: %s", exc)
                raw = {}
        if isinstance(raw, dict):
            for profile_id, rec in raw.items():
                if not isinstance(rec, dict):
                    continue
                pid = rec.get("pid")
                if pid and _pid_alive(pid, rec.get("spawned_at")):
                    _own_launches[profile_id] = dict(rec)
    return _own_launches


def _persist_own_launches() -> None:
    """Best-effort mirror of `_own_launches` to disk. Never raises — this is
    a courtesy for surviving a restart, not load-bearing for launch/stop
    correctness within one run."""
    try:
        path = _launch_state_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        atomic_write_json(path, dict(_own_launches), indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not persist launch_state.json: %s", exc)


def _forget_own_launch(profile_id: str) -> None:
    if _load_own_launches().pop(profile_id, None) is not None:
        _persist_own_launches()


def list_profiles() -> List[Dict[str, Any]]:
    with _STORE_LOCK:
        return [dict(p) for p in _load()["profiles"]]


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    with _STORE_LOCK:
        for p in _load()["profiles"]:
            if p.get("id") == profile_id:
                return dict(p)
    return None


def _validate_executable_path(path: str) -> Optional[str]:
    """The one reason `path` is not an acceptable executable, or None.

    Same rule everywhere an executable is validated (a profile's own
    `executable`, and a `stop_cmd.executable`): an absolute file that exists,
    or — Windows only — a bare name `shutil.which` resolves on PATH (e.g.
    `powershell.exe`, which has no fixed absolute location across Windows
    versions). POSIX keeps the stricter absolute-path-only rule: PATH lookup
    there would accept far more than the "an admin picked this exact file"
    guarantee the rest of this module relies on, and nothing in the F1.4
    contract needs it off Windows."""
    if not path:
        return "executable is required"
    if os.path.isabs(path):
        if not os.path.isfile(path):
            return f"executable not found: {path}"
        return None
    if IS_WINDOWS and shutil.which(path):
        return None
    return "executable must be an absolute path" + (
        " or a name resolvable on PATH" if IS_WINDOWS else ""
    )


def _validate_icon(icon: Any) -> Optional[str]:
    if not icon:
        return None
    if not isinstance(icon, str):
        return "icon must be a string path"
    if not os.path.isabs(icon):
        return "icon must be an absolute path"
    ext = os.path.splitext(icon)[1].lower()
    if ext not in ICON_CONTENT_TYPES:
        return f"icon must be one of {tuple(ICON_CONTENT_TYPES)}"
    if not os.path.isfile(icon):
        return f"icon not found: {icon}"
    try:
        size = os.path.getsize(icon)
    except OSError:
        return f"icon not found: {icon}"
    if size > ICON_MAX_BYTES:
        return f"icon too large (max {ICON_MAX_BYTES} bytes)"
    return None


def _validate_open_url(url: Any) -> Optional[str]:
    if not url:
        return None
    if not isinstance(url, str) or not _OPEN_URL_RE.match(url):
        return "open_url must be an http(s) URL"
    return None


def _validate_description(description: Any) -> Optional[str]:
    if description is None:
        return None
    if not isinstance(description, str):
        return "description must be a string"
    if len(description) > DESCRIPTION_MAX_CHARS:
        return f"description must be at most {DESCRIPTION_MAX_CHARS} characters"
    return None


def _validate_desktop(desktop: Any) -> Optional[str]:
    if desktop is None:
        return None
    if not isinstance(desktop, bool):
        return "desktop must be a boolean"
    return None


def _default_desktop(fields: Dict[str, Any]) -> bool:
    """Lot D: a profile opens in its own desktop window by default whenever
    it HAS somewhere to point that window — `open_url`, or a readiness URL
    with an http(s) origin. A profile with neither (e.g. a bare `open_exe`
    with no readiness check) has nothing a desktop shell could show, so it
    defaults to False instead."""
    if fields.get("open_url"):
        return True
    readiness = fields.get("readiness") or {}
    url = readiness.get("url")
    if url:
        try:
            parts = urlsplit(url)
        except Exception:  # noqa: BLE001
            return False
        return bool(parts.scheme in ("http", "https") and parts.netloc)
    return False


def _validate_stop_cmd(stop_cmd: Any) -> Optional[str]:
    if not stop_cmd:
        return None
    if not isinstance(stop_cmd, dict):
        return "stop_cmd must be an object"
    reason = _validate_executable_path(stop_cmd.get("executable") or "")
    if reason:
        return f"stop_cmd.{reason}"
    argv = stop_cmd.get("argv") if stop_cmd.get("argv") is not None else []
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return "stop_cmd.argv must be a list of strings"
    cwd = stop_cmd.get("cwd") or ""
    if cwd:
        if not os.path.isabs(cwd):
            return "stop_cmd.cwd must be an absolute path"
        if not os.path.isdir(cwd):
            return f"stop_cmd.cwd is not a directory: {cwd}"
    return None


def _validate(fields: Dict[str, Any]) -> List[str]:
    """Every reason this profile cannot be saved (or launched). Empty means
    valid. Same checks both times — F1.4: "Validación al guardar y al
    lanzar"."""
    reasons: List[str] = []
    kind = fields.get("kind")
    if kind not in KINDS:
        reasons.append(f"kind must be one of {KINDS}")

    executable = fields.get("executable") or ""
    if kind in ("process", "open_exe"):
        reason = _validate_executable_path(executable)
        if reason:
            reasons.append(reason)

    cwd = fields.get("cwd") or ""
    if kind == "process":
        if not cwd:
            reasons.append("cwd is required")
        elif not os.path.isabs(cwd):
            reasons.append("cwd must be an absolute path")
        elif not os.path.isdir(cwd):
            reasons.append(f"cwd is not a directory: {cwd}")

    argv = fields.get("argv") if fields.get("argv") is not None else []
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        reasons.append("argv must be a list of strings")
    else:
        for a in argv:
            if "\n" in a:
                reasons.append("argv entries may not contain newlines")
                break

    env = fields.get("env") if fields.get("env") is not None else {}
    if not isinstance(env, dict):
        reasons.append("env must be an object")
    else:
        for k in env:
            if not _ENV_KEY_RE.match(str(k)):
                reasons.append(f"invalid env key: {k!r} (must match [A-Z_][A-Z0-9_]*)")

    if kind == "open_url" and not fields.get("url") and not (fields.get("readiness") or {}).get("url"):
        reasons.append("open_url profiles need a url")

    readiness = fields.get("readiness") or {}
    if readiness and not isinstance(readiness, dict):
        reasons.append("readiness must be an object")

    for reason in (
        _validate_icon(fields.get("icon")),
        _validate_open_url(fields.get("open_url")),
        _validate_description(fields.get("description")),
        _validate_stop_cmd(fields.get("stop_cmd")),
        _validate_desktop(fields.get("desktop")),
    ):
        if reason:
            reasons.append(reason)

    return reasons


def create_profile(*, owner: Optional[str], name: str, kind: str, executable: str = "",
                    argv: Optional[List[str]] = None, cwd: str = "",
                    env: Optional[Dict[str, str]] = None,
                    readiness: Optional[Dict[str, Any]] = None,
                    url: Optional[str] = None, icon: Optional[str] = None,
                    open_url: Optional[str] = None,
                    stop_cmd: Optional[Dict[str, Any]] = None,
                    description: Optional[str] = None,
                    desktop: Optional[bool] = None) -> Dict[str, Any]:
    fields = {
        "name": name, "kind": kind, "executable": executable,
        "argv": list(argv or []), "cwd": cwd, "env": dict(env or {}),
        "readiness": dict(readiness or {}), "url": url,
        "icon": icon or None, "open_url": open_url or None,
        "stop_cmd": dict(stop_cmd) if stop_cmd else None,
        "description": description or None,
        # lot D: `desktop` defaults on whenever the profile has somewhere for
        # a desktop window to point (see `_default_desktop`); an explicit
        # True/False from the caller always wins over that default.
        "desktop": bool(desktop) if desktop is not None else _default_desktop({
            "open_url": open_url, "readiness": readiness,
        }),
    }
    reasons = _validate(fields)
    if reasons:
        raise ProfileValidationError("; ".join(reasons))
    now = time.time()
    entry = {"id": str(uuid.uuid4()), "owner": owner or None, **fields,
             "created_at": now, "updated_at": now}
    with _STORE_LOCK:
        data = _load()
        data["profiles"].append(entry)
        _save(data)
    return dict(entry)


def update_profile(profile_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    with _STORE_LOCK:
        data = _load()
        for p in data["profiles"]:
            if p.get("id") == profile_id:
                candidate = {**p, **fields}
                reasons = _validate(candidate)
                if reasons:
                    raise ProfileValidationError("; ".join(reasons))
                p.update(fields)
                p["updated_at"] = time.time()
                _save(data)
                return dict(p)
    return None


def delete_profile(profile_id: str) -> bool:
    with _STORE_LOCK:
        data = _load()
        before = len(data["profiles"])
        data["profiles"] = [p for p in data["profiles"] if p.get("id") != profile_id]
        if len(data["profiles"]) == before:
            return False
        _save(data)
    _forget_own_launch(profile_id)
    return True


# ── loopback ─────────────────────────────────────────────────────────────

def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def loopback_reason(url: str, request_client_host: Optional[str]) -> Optional[str]:
    """F1.4: a readiness/open URL that points at loopback is meaningless to
    anyone but the Faustus host itself. When the caller told us the request
    came from somewhere else, say so instead of silently handing back a URL
    that will never work for them."""
    if not url or not request_client_host:
        return None
    try:
        host = urlsplit(url).hostname or ""
    except Exception:  # noqa: BLE001
        return None
    if _is_loopback_host(host) and not _is_loopback_host(request_client_host):
        return ("This URL is loopback on the Faustus host; configure the real address")
    return None


# ── readiness ────────────────────────────────────────────────────────────

async def _check_ready_once(readiness: Dict[str, Any]) -> bool:
    url = (readiness or {}).get("url")
    if not url:
        return False
    expect = (readiness or {}).get("expect") or {}
    import httpx
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=2.0) as client:
            resp = await client.get(url)
    except Exception:  # noqa: BLE001
        return False
    if resp.status_code != 200:
        return False
    if not expect:
        return True
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return False
    return all(body.get(k) == v for k, v in expect.items())


def _pid_alive(pid: int, spawned_at: Optional[float]) -> bool:
    try:
        import psutil
        proc = psutil.Process(pid)
        if spawned_at is None:
            return proc.is_running()
        return proc.is_running() and abs(float(proc.create_time()) - spawned_at) <= 1.0
    except Exception:  # noqa: BLE001
        return False


def _profile_lock(profile_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _profile_locks.setdefault(profile_id, threading.Lock())
    return lock


# ── launch ───────────────────────────────────────────────────────────────

async def launch(profile_id: str, *, request_client_host: Optional[str] = None) -> Dict[str, Any]:
    """Run `profile_id`'s launch profile. Idempotent: an already-running app
    (proved by readiness, or by a pid this process itself started and can
    still see alive) is reported, never re-launched. Never kills anything —
    "al parar Faustus no se terminan estos procesos" applies just as much to
    a launch profile deciding not to act."""
    profile = get_profile(profile_id)
    if profile is None:
        return {"launched": False, "error": f"no such launch profile: {profile_id}"}

    reasons = _validate(profile)
    if reasons:
        return {"launched": False, "error": "; ".join(reasons)}

    kind = profile["kind"]
    readiness = profile.get("readiness") or {}
    reasons_out: List[str] = []

    if kind == "open_url":
        url = profile.get("url") or readiness.get("url")
        note = loopback_reason(url, request_client_host)
        if note:
            reasons_out.append(note)
        out = {"launched": False, "kind": "url", "url": url, "reasons": reasons_out}
        # Lot D: "for open_url kinds: open the shell instead of returning the
        # URL -- but KEEP returning the url fields for compatibility": the
        # response shape here is unchanged from before lot D, on purpose —
        # opening the desktop window is a side effect, not a new field a
        # caller now has to know about. `status(profile_id)["desktop_open"]`
        # is where a caller observes whether it actually opened.
        _maybe_open_desktop(profile, profile_id)
        return out

    # `_maybe_open_desktop` -> `open_desktop` takes this SAME per-profile
    # lock itself (it needs to, to stay idempotent against a concurrent
    # `open_desktop` call) — every call to it below is therefore made AFTER
    # the `with lock:` block has exited, never from inside it, or a single
    # launch would deadlock itself on its own non-reentrant lock.
    early_out: Optional[Dict[str, Any]] = None
    open_exe_out: Optional[Dict[str, Any]] = None
    result: Optional[LaunchResult] = None

    lock = _profile_lock(profile_id)
    with lock:
        own = _load_own_launches().get(profile_id)
        if own and _pid_alive(own["pid"], own.get("spawned_at")):
            early_out = {"launched": False, "already_running": True, "pid": own["pid"]}
        elif readiness.get("url") and await _check_ready_once(readiness):
            early_out = {"launched": False, "already_running": True}
        else:
            argv = [profile["executable"], *profile.get("argv", [])]
            cwd = profile.get("cwd") or os.path.dirname(profile["executable"]) or "."
            env = {**os.environ, **(profile.get("env") or {})}
            log_path = os.path.join(_log_dir(), f"{profile_id}.log")
            result = spawn_detached(
                argv, cwd=cwd, env=env, log_path=log_path, owner=f"launch_profile:{profile_id}",
            )
            # Merge (not replace): a previous `open_desktop()` may have left
            # a `desktop_pid`/`desktop_spawned_at` pair in this same record,
            # and a fresh spawn of the APP must not clobber tracking of its
            # own shell window.
            existing = dict(_own_launches.get(profile_id) or {})
            existing.update({
                "pid": result.pid, "spawned_at": result.spawned_at, "log_path": result.log_path,
            })
            _own_launches[profile_id] = existing
            _persist_own_launches()
            if kind == "open_exe":
                # No readiness by design (F1.4: "lanza el ejecutable
                # aprobado sin readiness") — a desktop GUI app has nothing
                # to poll.
                open_exe_out = {"launched": True, "pid": result.pid, "log_path": result.log_path}

    if early_out is not None:
        # Side effect only, same reasoning as the open_url branch above: the
        # response shape stays exactly what it was before lot D.
        _maybe_open_desktop(profile, profile_id)
        return early_out
    if open_exe_out is not None:
        return open_exe_out

    # Opened right after spawning the app: the shell's own splash screen
    # (desktop/app-shell.cjs) polls `--url` every second for up to 60s on
    # its own, so opening it here need not wait for this function's
    # readiness poll below to finish first. Side effect only — see above.
    _maybe_open_desktop(profile, profile_id)

    # kind == "process": poll readiness OUTSIDE the lock — a slow-starting
    # app must not hold the per-profile lock for the whole timeout window,
    # or a status check on the same profile would wait behind it.
    ready = False
    if readiness.get("url"):
        deadline = time.monotonic() + float(readiness.get("timeout_s") or 20)
        while time.monotonic() < deadline:
            if await _check_ready_once(readiness):
                ready = True
                break
            import asyncio
            await asyncio.sleep(0.5)
        note = loopback_reason(readiness.get("url"), request_client_host)
        if note:
            reasons_out.append(note)

    out = {"launched": True, "pid": result.pid, "log_path": result.log_path, "ready": ready}
    if reasons_out:
        out["reasons"] = reasons_out
    if not ready and readiness.get("url"):
        out["detail"] = "process started but did not answer its readiness URL in time"
    return out


# ── status ───────────────────────────────────────────────────────────────

def _extract_port(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    try:
        return urlsplit(url).port
    except Exception:  # noqa: BLE001
        return None


def _pid_cmdline(pid: Optional[int]) -> Optional[str]:
    if not pid:
        return None
    try:
        import psutil
        cmdline = " ".join(psutil.Process(int(pid)).cmdline() or [])[:300]
        return cmdline or None
    except Exception:  # noqa: BLE001
        return None


def _profile_readiness_url(profile: Dict[str, Any]) -> Optional[str]:
    readiness = profile.get("readiness") or {}
    if readiness.get("url"):
        return readiness["url"]
    if profile.get("kind") == "open_url":
        return profile.get("url")
    return None


def _desktop_open(profile_id: Optional[str]) -> bool:
    """Whether a desktop shell window this Faustus run (or a prior one, via
    `launch_state.json`) opened for `profile_id` is still alive."""
    if not profile_id:
        return False
    own = _load_own_launches().get(profile_id) or {}
    pid = own.get("desktop_pid")
    return bool(pid and _pid_alive(pid, own.get("desktop_spawned_at")))


async def _status_for(profile: Dict[str, Any], *,
                       ports_by_pid: Optional[Dict[int, List[int]]] = None) -> Dict[str, Any]:
    """One profile's status. Never raises. Shared by `status()` (one profile)
    and `list_statuses()` (every profile against one `_ports_by_pid()` scan,
    passed in as `ports_by_pid`) so the two never disagree."""
    profile_id = profile.get("id")
    readiness = profile.get("readiness") or {}
    url = _profile_readiness_url(profile)
    port = _extract_port(url)
    out: Dict[str, Any] = {
        "running": False, "source": "none", "pid": None, "created_at": None,
        "port": port, "ready": False, "url": url, "pid_command": None,
        "desktop_open": _desktop_open(profile_id),
    }

    own = _load_own_launches().get(profile_id) if profile_id else None
    if own and _pid_alive(own.get("pid"), own.get("spawned_at")):
        out["running"] = True
        out["source"] = "owned"
        out["pid"] = own.get("pid")
        out["created_at"] = own.get("spawned_at")
        out["pid_command"] = _pid_cmdline(own.get("pid"))
        out["ready"] = await _check_ready_once(readiness) if readiness.get("url") else True
        return out

    if readiness.get("url") and await _check_ready_once(readiness):
        out["running"] = True
        out["ready"] = True
        out["source"] = "readiness"
        return out

    if port:
        from src import process_center
        found = process_center.pid_listening_on(port, ports_by_pid=ports_by_pid)
        if found:
            out["running"] = True
            out["ready"] = True
            out["source"] = "port"
            out["pid"] = found.get("pid")
            out["created_at"] = found.get("created_at")
            out["pid_command"] = found.get("cmdline") or None
            return out

    return out


async def status(profile_id: str) -> Dict[str, Any]:
    """`{running, source, pid, created_at, port, ready, url, pid_command}`
    for one profile. `source` is `"owned"` (a pid this Faustus run — or a
    prior one, via `launch_state.json` — spawned and can still see alive),
    `"port"` (something else now listens on the readiness URL's port),
    `"readiness"` (the readiness URL answers but nothing local proves whose
    process that is), or `"none"`. Never raises."""
    profile = get_profile(profile_id)
    if profile is None:
        return {"running": False, "source": "none", "pid": None, "created_at": None,
                "port": None, "ready": False, "url": None, "pid_command": None,
                "desktop_open": False}
    return await _status_for(profile)


async def list_statuses() -> Dict[str, Dict[str, Any]]:
    """`status()` for every profile, in one `_ports_by_pid()` scan."""
    from src import process_center
    profiles = list_profiles()
    ports_by_pid = process_center._ports_by_pid()
    out: Dict[str, Dict[str, Any]] = {}
    for profile in profiles:
        out[profile["id"]] = await _status_for(profile, ports_by_pid=ports_by_pid)
    return out


# ── stop / restart ──────────────────────────────────────────────────────────

async def _wait_until_not_running(profile: Dict[str, Any], timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        st = await _status_for(profile)
        if not st.get("running"):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.3)


def _run_stop_cmd_sync(stop_cmd: Dict[str, Any]) -> bool:
    import subprocess
    argv = [stop_cmd["executable"], *stop_cmd.get("argv", [])]
    cwd = stop_cmd.get("cwd") or None
    try:
        proc = subprocess.run(argv, cwd=cwd, shell=False, timeout=30, capture_output=True)
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("[launch_profiles] stop_cmd failed: %s", exc)
        return False


async def stop(profile_id: str, *, allow_external: bool = True) -> Dict[str, Any]:
    """Stop `profile_id`'s app, in order: `stop_cmd` if the profile has one
    (bounded to 30 s, `shell=False`, verified by readiness/pid/port going
    away afterwards); else a pid this Faustus run owns
    (`process_center.stop`, which already refuses protected processes such
    as Ollama); else — when `allow_external` — whatever now owns the
    readiness URL's port (`process_center.stop_port`), for the case where
    the app is running but Faustus never started it. Result:
    ``{"stopped": bool, "how": "stop_cmd"|"owned"|"port"|"not_running",
    "reason"?}``. Waits up to 8 s for the app to actually go away before
    reporting success. Never raises."""
    profile = get_profile(profile_id)
    if profile is None:
        return {"stopped": False, "how": "not_running", "reason": "no such launch profile"}

    lock = _profile_lock(profile_id)
    with lock:
        # Lot D: a desktop shell window open for this profile is stopped
        # right alongside the app it shows, whichever way the app itself
        # turns out to be stopped below (or even if it was not running at
        # all — a window pointed at an app that just stopped answering has
        # nothing useful left to show).
        _stop_desktop(profile_id)
        stop_cmd = profile.get("stop_cmd")
        if stop_cmd:
            reason = _validate_stop_cmd(stop_cmd)
            if reason:
                return {"stopped": False, "how": "stop_cmd", "reason": reason}
            loop = asyncio.get_event_loop()
            ran_ok = await loop.run_in_executor(None, _run_stop_cmd_sync, stop_cmd)
            verified = await _wait_until_not_running(profile, 8)
            _forget_own_launch(profile_id)
            stopped = bool(ran_ok) and verified
            out: Dict[str, Any] = {"stopped": stopped, "how": "stop_cmd"}
            if not stopped:
                out["reason"] = (
                    "the app is still answering after stop_cmd ran" if ran_ok
                    else "stop_cmd exited with a non-zero status"
                )
            return out

        own = _load_own_launches().get(profile_id)
        if own and _pid_alive(own.get("pid"), own.get("spawned_at")):
            from src import process_center
            result = process_center.stop(own.get("pid"), own.get("spawned_at"))
            stopped = bool(result.get("ok"))
            if stopped:
                _forget_own_launch(profile_id)
                await _wait_until_not_running(profile, 8)
            return {"stopped": stopped, "how": "owned", "reason": result.get("reason") or ""}

        if not allow_external:
            return {"stopped": False, "how": "not_running"}

        port = _extract_port(_profile_readiness_url(profile))
        if port:
            from src import process_center
            found = process_center.pid_listening_on(port)
            if found:
                result = process_center.stop_port(port)
                stopped = bool(result.get("ok"))
                if stopped:
                    await _wait_until_not_running(profile, 8)
                return {"stopped": stopped, "how": "port", "reason": result.get("reason") or ""}

        return {"stopped": False, "how": "not_running"}


async def restart(profile_id: str) -> Dict[str, Any]:
    """`stop()` → wait until the port is free (max 10 s) → `launch()`. The
    result merges both: `stopped`/`how`/`reason` from the stop half,
    `launched`/`pid`/`log_path`/`ready`/... from the launch half — plus
    nested `stop`/`launch` for a caller that wants them apart."""
    profile = get_profile(profile_id)
    if profile is None:
        return {"launched": False, "stopped": False, "error": f"no such launch profile: {profile_id}"}
    stop_result = await stop(profile_id)
    await _wait_until_not_running(profile, 10)
    launch_result = await launch(profile_id)
    return {**stop_result, **launch_result, "stop": stop_result, "launch": launch_result}


# ── icon / log ───────────────────────────────────────────────────────────

def icon_bytes(profile_id: str) -> Optional[Dict[str, Any]]:
    """`{"data": bytes, "content_type": str}` for `profile_id`'s icon, or
    None when the profile has no icon, the file is gone, or its extension is
    not one of `ICON_CONTENT_TYPES` — re-checked here, not just at save
    time, in case the file on disk changed since."""
    profile = get_profile(profile_id)
    if profile is None:
        return None
    icon = profile.get("icon")
    if not icon or not isinstance(icon, str):
        return None
    ext = os.path.splitext(icon)[1].lower()
    content_type = ICON_CONTENT_TYPES.get(ext)
    if content_type is None:
        return None
    if not os.path.isabs(icon) or not os.path.isfile(icon):
        return None
    try:
        with open(icon, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if len(data) > ICON_MAX_BYTES:
        return None
    return {"data": data, "content_type": content_type}


def _desktop_target_url(profile: Dict[str, Any]) -> Optional[str]:
    """The URL the desktop shell should show for `profile`: `open_url` when
    set, else the ORIGIN of the readiness URL (a readiness check often
    points at `/api/health`, not at the page a human should look at), else
    — for an `open_url`-kind profile with no `open_url` field filled in yet
    — its own `url`. None when there is nowhere to point a window."""
    open_url = profile.get("open_url")
    if open_url:
        return open_url
    readiness = profile.get("readiness") or {}
    url = readiness.get("url")
    if url:
        try:
            parts = urlsplit(url)
        except Exception:  # noqa: BLE001
            parts = None
        if parts and parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    if profile.get("kind") == "open_url":
        return profile.get("url")
    return None


def _electron_binary() -> Optional[str]:
    """The Electron binary lot D's shell runs under: Faustus's own
    `desktop/node_modules/electron` (the checkout this module lives in, same
    as `desktop/main.cjs` itself runs under) — never a system-wide Electron,
    which may not exist or may be a different version. `path.txt` inside
    that package names the actual binary file under `dist/` the same way
    `npm`'s own `electron` package resolves it (see its `index.js`); we read
    it the same way rather than hard-coding a name that could drift with an
    Electron upgrade, falling back to the conventional
    `electron.exe`/`electron` name when the file is absent."""
    electron_dir = os.path.join(BASE_DIR, "desktop", "node_modules", "electron")
    dist_dir = os.path.join(electron_dir, "dist")
    path_txt = os.path.join(electron_dir, "path.txt")
    name = None
    if os.path.isfile(path_txt):
        try:
            with open(path_txt, "r", encoding="utf-8") as fh:
                name = fh.read().strip() or None
        except OSError:
            name = None
    if not name:
        name = "electron.exe" if IS_WINDOWS else "electron"
    binary = os.path.join(dist_dir, name)
    return binary if os.path.isfile(binary) else None


def open_desktop(profile_id: str) -> Dict[str, Any]:
    """Open `profile_id`'s app in its own desktop window
    (`desktop/app-shell.cjs`), spawned detached the same way a launch
    profile's own process is. Idempotent: while a shell window we spawned
    for this profile is still alive, a second call reports
    ``{"opened": False, "already_open": True}`` instead of opening a second
    window. Never raises."""
    profile = get_profile(profile_id)
    if profile is None:
        return {"opened": False, "error": f"no such launch profile: {profile_id}"}

    target_url = _desktop_target_url(profile)
    if not target_url:
        return {"opened": False, "error": "no open_url or readiness url configured for a desktop window"}

    lock = _profile_lock(profile_id)
    with lock:
        own = dict(_load_own_launches().get(profile_id) or {})
        shell_pid = own.get("desktop_pid")
        if shell_pid and _pid_alive(shell_pid, own.get("desktop_spawned_at")):
            return {"opened": False, "already_open": True, "pid": shell_pid}

        binary = _electron_binary()
        if not binary:
            return {"opened": False, "error": "desktop runtime not installed (desktop/: npm install)"}

        app_shell = os.path.join(BASE_DIR, "desktop", "app-shell.cjs")
        argv = [
            binary, app_shell,
            f"--url={target_url}", f"--title={profile.get('name') or profile_id}",
            f"--slug={profile_id}",
        ]
        icon = profile.get("icon")
        if icon:
            argv.append(f"--icon={icon}")
        # Never let this spawn run as a plain Node script instead of the
        # Electron app it is — some shells leak ELECTRON_RUN_AS_NODE into a
        # child's environment, which would make `binary app-shell.cjs`
        # execute as bare Node (no `app`/`BrowserWindow` available at all).
        env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
        log_path = os.path.join(_log_dir(), f"{profile_id}.desktop.log")
        result = spawn_detached(
            argv, cwd=os.path.join(BASE_DIR, "desktop"), env=env, log_path=log_path,
            owner=f"desktop_shell:{profile_id}",
        )
        own["desktop_pid"] = result.pid
        own["desktop_spawned_at"] = result.spawned_at
        _own_launches[profile_id] = own
        _persist_own_launches()
        return {"opened": True, "pid": result.pid, "log_path": result.log_path}


def _maybe_open_desktop(profile: Dict[str, Any], profile_id: str) -> Optional[Dict[str, Any]]:
    """`open_desktop(profile_id)` when `profile["desktop"]` is set, else
    None (so callers can decide whether to merge a `"desktop"` key into
    their own result at all)."""
    if not profile.get("desktop"):
        return None
    return open_desktop(profile_id)


def _stop_desktop(profile_id: str) -> None:
    """Best-effort: close `profile_id`'s desktop shell window (if any) as
    part of `stop()` — the app being stopped is a good reason to also close
    a window that was just showing it, even though the window itself never
    stops the app on its own close (see `desktop/app-shell.cjs`). Never
    raises; a failure here must not make `stop()` report failure for the app
    it actually stopped."""
    own = dict(_load_own_launches().get(profile_id) or {})
    pid = own.pop("desktop_pid", None)
    spawned_at = own.pop("desktop_spawned_at", None)
    if not pid:
        return
    try:
        if _pid_alive(pid, spawned_at):
            from src import process_center
            process_center.stop(pid, spawned_at)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[launch_profiles] could not stop desktop shell for %s: %s", profile_id, exc)
    if own:
        _own_launches[profile_id] = own
    else:
        _own_launches.pop(profile_id, None)
    _persist_own_launches()


def log_tail(profile_id: str, *, lines: int = 200) -> Optional[str]:
    """The last `lines` lines of `profile_id`'s launch log, `""` when it has
    not logged anything yet, or None when the profile does not exist."""
    profile = get_profile(profile_id)
    if profile is None:
        return None
    own = _load_own_launches().get(profile_id) or {}
    log_path = own.get("log_path") or os.path.join(_log_dir(), f"{profile_id}.log")
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.readlines()
    except OSError:
        return ""
    n = max(1, int(lines))
    return "".join(data[-n:])
