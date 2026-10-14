"""Start the local Ollama server when a call needs it and it is not running.

Local models served by Ollama on this machine used to fail the turn with
«Cannot reach 127.0.0.1:11434» whenever the tray app was closed; the person
had to start it by hand and send the message again. `ensure(url)` is called
from `src.engine_swap.ensure_ready` before every local model call (and again
after a connection failure): when `url` is the loopback Ollama port, the port
is closed and the `ollama` binary is installed, it runs `ollama serve`
detached and waits for the port. Never raises; a failure falls through to the
call's own clear connection error. Setting: `ollama_autostart` (on).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
_FAIL_COOLDOWN_S = 60.0
_lock: Optional[asyncio.Lock] = None
_last_failure = 0.0


def _enabled() -> bool:
    # A test run never launches the real server as a side effect.
    if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get("FAUSTUS_TEST_OLLAMA_AUTOSTART"):
        return False
    try:
        from src.settings import get_setting
        return bool(get_setting("ollama_autostart", True))
    except Exception:
        return True


def ollama_port() -> int:
    host = os.environ.get("OLLAMA_HOST", "")
    if host:
        parsed = urlparse(host if "://" in host else "http://" + host)
        if parsed.port:
            return parsed.port
    return 11434


def is_local_ollama_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    return (parsed.hostname or "") in _LOOPBACK and parsed.port == ollama_port()


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def find_binary() -> Optional[str]:
    found = shutil.which("ollama")
    if found:
        return found
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        candidate = os.path.join(local, "Programs", "Ollama", "ollama.exe")
        if local and os.path.isfile(candidate):
            return candidate
    for candidate in ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/usr/bin/ollama"):
        if os.path.isfile(candidate):
            return candidate
    return None


def _registry_ollama_env() -> Dict[str, str]:
    """OLLAMA_* variables saved for the machine and the user on Windows.

    The server process may have been started before they were set (or from
    a shell that never saw them): an `ollama serve` with only our own
    environment then looks in the default models folder and lists no models
    at all (seen live: OLLAMA_MODELS=D:\\LocalAI\\ollama-models). The user's
    values win over the machine's, like a fresh login."""
    if sys.platform != "win32":
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    found: Dict[str, str] = {}
    hives = (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    )
    for hive, path in hives:
        try:
            with winreg.OpenKey(hive, path) as key:
                index = 0
                while True:
                    try:
                        name, value, _kind = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if str(name).upper().startswith("OLLAMA_") and isinstance(value, str):
                        found[str(name).upper()] = os.path.expandvars(value)
        except OSError:
            continue
    return found


def _spawn_env() -> Dict[str, str]:
    env = dict(os.environ)
    for name, value in _registry_ollama_env().items():
        env.setdefault(name, value)
    return env


def _spawn(binary: str) -> None:
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL, "close_fds": True, "env": _spawn_env(),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x8)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x8000000)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([binary, "serve"], **kwargs)  # noqa: S603 - fixed argv, local binary


async def ensure(url: str, *, timeout_s: float = 20.0) -> Dict[str, Any]:
    """Start Ollama for `url` if needed. Returns {"action": none|started|failed}."""
    global _lock, _last_failure
    try:
        if not is_local_ollama_url(url) or not _enabled():
            return {"action": "none"}
        port = ollama_port()
        if await asyncio.to_thread(_port_open, port):
            return {"action": "none"}
        if time.monotonic() - _last_failure < _FAIL_COOLDOWN_S:
            return {"action": "failed", "error": "recent start attempt failed"}
        binary = find_binary()
        if not binary:
            return {"action": "none", "reason": "ollama is not installed"}
        if _lock is None:
            _lock = asyncio.Lock()
        async with _lock:
            if await asyncio.to_thread(_port_open, port):
                return {"action": "none"}
            logger.warning("[ollama] %s is closed; starting `ollama serve`", url)
            await asyncio.to_thread(_spawn, binary)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                if await asyncio.to_thread(_port_open, port):
                    logger.info("[ollama] serving on port %s", port)
                    return {"action": "started"}
            _last_failure = time.monotonic()
            return {"action": "failed", "error": f"port {port} still closed after {timeout_s:.0f}s"}
    except Exception as exc:  # noqa: BLE001 - never break the call
        _last_failure = time.monotonic()
        logger.warning("[ollama] autostart failed: %s", exc)
        return {"action": "failed", "error": str(exc)}
