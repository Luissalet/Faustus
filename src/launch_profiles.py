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

import ipaddress
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from core.atomic_io import atomic_write_json
from core.platform_compat import safe_chmod
from src.constants import DATA_DIR as _DEFAULT_DATA_DIR
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

_STORE_LOCK = threading.Lock()
#: One lock per profile_id, created on first use. Guarded by `_locks_guard`
#: only for the dict access itself, never held while the per-profile lock is.
_locks_guard = threading.Lock()
_profile_locks: Dict[str, threading.Lock] = {}

#: In-process record of a profile this Faustus process itself started:
#: profile_id -> {"pid", "spawned_at", "log_path"}. Deliberately NOT
#: persisted to disk — the cross-restart idempotency signal is the
#: readiness URL (the domain app is still listening whether or not Faustus
#: remembers spawning it); this dict only has to survive within one Faustus
#: run, to catch a same-process double click before readiness has had a
#: chance to come up yet.
_own_launches: Dict[str, Dict[str, Any]] = {}


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


def list_profiles() -> List[Dict[str, Any]]:
    with _STORE_LOCK:
        return [dict(p) for p in _load()["profiles"]]


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    with _STORE_LOCK:
        for p in _load()["profiles"]:
            if p.get("id") == profile_id:
                return dict(p)
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
        if not executable:
            reasons.append("executable is required")
        elif not os.path.isabs(executable):
            reasons.append("executable must be an absolute path")
        elif not os.path.isfile(executable):
            reasons.append(f"executable not found: {executable}")

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

    return reasons


def create_profile(*, owner: Optional[str], name: str, kind: str, executable: str = "",
                    argv: Optional[List[str]] = None, cwd: str = "",
                    env: Optional[Dict[str, str]] = None,
                    readiness: Optional[Dict[str, Any]] = None,
                    url: Optional[str] = None) -> Dict[str, Any]:
    fields = {
        "name": name, "kind": kind, "executable": executable,
        "argv": list(argv or []), "cwd": cwd, "env": dict(env or {}),
        "readiness": dict(readiness or {}), "url": url,
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
    _own_launches.pop(profile_id, None)
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
        return {"launched": False, "kind": "url", "url": url, "reasons": reasons_out}

    lock = _profile_lock(profile_id)
    with lock:
        own = _own_launches.get(profile_id)
        if own and _pid_alive(own["pid"], own.get("spawned_at")):
            return {"launched": False, "already_running": True, "pid": own["pid"]}

        if readiness.get("url") and await _check_ready_once(readiness):
            return {"launched": False, "already_running": True}

        argv = [profile["executable"], *profile.get("argv", [])]
        cwd = profile.get("cwd") or os.path.dirname(profile["executable"]) or "."
        env = {**os.environ, **(profile.get("env") or {})}
        log_path = os.path.join(_log_dir(), f"{profile_id}.log")
        result: LaunchResult = spawn_detached(
            argv, cwd=cwd, env=env, log_path=log_path, owner=f"launch_profile:{profile_id}",
        )
        _own_launches[profile_id] = {
            "pid": result.pid, "spawned_at": result.spawned_at, "log_path": result.log_path,
        }

        if kind == "open_exe":
            # No readiness by design (F1.4: "lanza el ejecutable aprobado sin
            # readiness") — a desktop GUI app has nothing to poll.
            return {"launched": True, "pid": result.pid, "log_path": result.log_path}

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
