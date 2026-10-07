"""Bring a local model server back when it is stuck answering garbage.

Seen live several times (25-09 to 01-10): a `llama-server` on this machine
starts answering every prompt -- even "say hello" -- with one symbol repeated
(``////``, ``????``) and stays that way until it is restarted. Faustus's
stream guard already stops such a reply and says the server needs a restart;
this module does the restart instead of leaving it to the person.

What it does, for a loopback endpoint only:

1. Ask the server for a trivial arithmetic answer with reasoning off
   (`engine_swap.generates_sanely`), confirming an incorrect answer with a
   second probe. A correct answer ends here:
   a model looping on a real prompt is the harness's business, not a broken
   server.
2. Restart the server, the first way that applies:
   * a supervised server: the process listening on the port was started by a
     script that is still running (a launcher that starts the server again
     when it exits). Ending the server process is then a restart, with the
     command that started it -- a managed engine for the same port may
     describe a different model, so the live launcher goes first;
   * an identifiable unsupervised llama-server: relaunch its actual argv,
     working directory and environment, preserving the live model and flags;
   * a managed engine (Settings > engines): stop and start it;
   * a command set for that host:port in ``model_server_restart_commands``
     (``"127.0.0.1:8081=powershell -File D:\\LocalAI\\Restart-LlamaServer.ps1"``).
   A server with none of these is left alone and the result says why.
3. Wait for the port, ``/health`` and a correct generated answer again.

Several Faustus instances can share one server, so a file lock per port in
the temp folder makes the second one wait for the first instead of restarting
again, and a cooldown stops a server that comes back broken from being
restarted in a loop. ``model_server_auto_heal`` (on by default) switches it
all off. Never raises.

During Faustus's lifespan, a background check every two minutes reads the
running default local endpoint's slots. It probes only when every slot is idle
and reports zero retained prompt tokens, so it does not evict a reusable chat
prefix. Missing or malformed slot state is skipped. Nothing is loaded by the
monitor; symptom-triggered recovery remains available through request-time paths.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Launchers that keep a server running: the parent of the listening process.
_SUPERVISOR_NAMES = frozenset({"powershell", "pwsh", "cmd", "bash", "sh", "zsh", "dash"})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
#: After a restart, no second one for this long (it would loop on a server
#: that comes back broken).
_COOLDOWN_S = 180.0

_INFLIGHT: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
_LAST_RESTART: Dict[str, float] = {}
_EVENTS: Deque[Dict[str, Any]] = deque(maxlen=20)
_MONITOR_TASK: Optional[asyncio.Task] = None
_CHECK_INTERVAL_S = 120.0


def _settings() -> Dict[str, Any]:
    try:
        from src.settings import get_setting
        raw = get_setting("model_server_restart_commands", []) or []
        return {
            "enabled": bool(get_setting("model_server_auto_heal", True)),
            "commands": list(raw) if isinstance(raw, (list, tuple)) else [],
            "timeout_s": float(get_setting("model_server_heal_timeout_s", 420) or 420),
        }
    except Exception:  # noqa: BLE001
        return {"enabled": True, "commands": [], "timeout_s": 420.0}


def _host_port(url: str) -> Optional[Tuple[str, int]]:
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    if host not in _LOOPBACK:
        return None
    return host, int(port)


def _base(url: str) -> str:
    parsed = urlparse(str(url or ""))
    return f"{parsed.scheme or 'http'}://{parsed.netloc}"


def command_for(port: int, commands: List[str]) -> Optional[str]:
    """The configured restart command for ``port`` (entries ``host:port=command``)."""
    for entry in commands:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        key, cmd = entry.split("=", 1)
        try:
            if int(key.strip().rsplit(":", 1)[-1]) == port and cmd.strip():
                return cmd.strip()
        except ValueError:
            continue
    return None


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:  # pragma: no cover - psutil is a requirement
        return None


def listener_pid(port: int) -> Optional[int]:
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port and conn.pid:
                return int(conn.pid)
    except Exception:  # noqa: BLE001 - access denied or platform gap
        return None
    return None


def supervisor_of(pid: int) -> Optional[Dict[str, Any]]:
    """The still-running launcher script that started ``pid``, or None.

    A script host must contain a continuous relaunch loop. An interactive shell,
    one-shot script, Faustus instance or
    anything else that started it is not a supervisor -- ending the server
    would just leave it down."""
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        parent = psutil.Process(pid).parent()
        if parent is None or not parent.is_running():
            return None
        name = os.path.splitext(parent.name() or "")[0].lower()
        if name not in _SUPERVISOR_NAMES:
            return None
        argv = parent.cmdline() or []
        if not _launcher_has_restart_loop(argv, parent.cwd()):
            return None
        cmd = " ".join(argv)
        return {"pid": parent.pid, "name": parent.name(), "cmdline": cmd[:300]}
    except Exception:  # noqa: BLE001
        return None


def _launcher_has_restart_loop(argv: List[str], cwd: str) -> bool:
    """An interactive shell or one-shot start script is not a watchdog."""
    import re
    body = " ".join(argv[1:])
    for i, arg in enumerate(argv[:-1]):
        if arg.lower() in {"-file", "-f"}:
            path = argv[i + 1]
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            try:
                with open(path, encoding="utf-8-sig") as script:
                    body = script.read(262144)
            except (OSError, UnicodeError):
                return False
            break
    # Require the relaunch loop, not simply a shell executable as parent.
    return bool(re.search(r"while\s*\(\s*\$true\s*\)|while\s+(?:true|:)\s*;|for\s*\(\s*;\s*;\s*\)",
                          body, re.IGNORECASE))


def _lock_path(port: int) -> str:
    return os.path.join(tempfile.gettempdir(), f"faustus-model-heal-{port}.lock")


def _take_lock(port: int, stale_after_s: float) -> bool:
    """One restart per port across Faustus instances. A lock older than
    ``stale_after_s`` belonged to a heal that died and is taken over."""
    path = _lock_path(port)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{os.getpid()} {time.time():.0f}\n")
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale_after_s:
                    os.remove(path)
                    continue
            except OSError:
                continue
            return False
        except OSError:
            return False
    return False


def _drop_lock(port: int) -> None:
    try:
        os.remove(_lock_path(port))
    except OSError:
        pass


def _record(event: Dict[str, Any]) -> Dict[str, Any]:
    event = {"at": time.time(), **event}
    _EVENTS.append(event)
    try:
        # A server confirmed to be answering garbage is a failure for the
        # unattended-work breaker (src/unattended_breaker.py).
        if event.get("action") in ("restarted", "failed", "manual", "waited"):
            from src import unattended_breaker
            unattended_breaker.record("local_garbage", False, str(event.get("detail") or "")[:200])
    except Exception:  # noqa: BLE001
        pass
    level = logging.WARNING if event.get("action") in ("restarted", "failed") else logging.INFO
    logger.log(level, "[model-heal] %s", event)
    return event


async def _health_ok(url: str) -> bool:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(_base(url) + "/health")
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def _server_slots(url: str) -> Optional[List[Dict[str, Any]]]:
    """Read a valid llama.cpp slot list, or None when its state is unknown."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(_base(url) + "/slots")
        if response.status_code != 200:
            return None
        slots = response.json()
        if not isinstance(slots, list) or not slots or any(
            not isinstance(slot, dict) or not isinstance(slot.get("is_processing"), bool)
            for slot in slots
        ):
            return None
        return slots
    except Exception:
        return None


async def _server_idle(url: str) -> Optional[bool]:
    """True only for a valid llama.cpp slot list with no processing request."""
    slots = await _server_slots(url)
    return None if slots is None else not any(slot["is_processing"] for slot in slots)


def _slots_have_empty_prefix(slots: List[Dict[str, Any]]) -> bool:
    """Periodic probes are safe only when every slot is idle and has no prefix."""
    for slot in slots:
        count = slot.get("n_prompt_tokens")
        if (slot.get("is_processing") is not False or isinstance(count, bool)
                or not isinstance(count, int) or count < 0 or count > 0):
            return False
    return bool(slots)


def _llama_snapshot(pid: int, port: int, model: str) -> Optional[Dict[str, Any]]:
    """Capture the live runner, never a registered profile for a different model.

    Keep argv and environment private. The PID's creation time is checked again
    immediately before termination so a reused PID cannot be restarted.
    """
    psutil = _psutil()
    if psutil is None:
        return None
    try:
        proc = psutil.Process(pid)
        executable = proc.exe()
        if os.path.basename(executable).lower() not in {"llama-server", "llama-server.exe"}:
            return None
        argv = proc.cmdline()
        from src.engines import _flag_value
        if int(_flag_value(argv, {"--port"}) or 8080) != port:
            return None
        aliases = [alias.strip() for alias in (_flag_value(argv, {"--alias", "-a"}) or "").split(",")]
        if not model or model not in aliases:
            return None
        return {"pid": pid, "created": proc.create_time(), "argv": [executable, *argv[1:]],
                "cwd": proc.cwd(), "env": proc.environ()}
    except Exception:
        return None


async def _restart_live_llama(spec: Dict[str, Any], port: int) -> bool:
    psutil = _psutil()
    if psutil is None:
        return False
    try:
        proc = psutil.Process(spec["pid"])
        if (proc.create_time() != spec["created"] or proc.exe() != spec["argv"][0]
                or proc.cmdline()[1:] != spec["argv"][1:]):
            return False
        if not await _end_process(spec["pid"]):
            return False
        # Nothing else must have claimed the port while the old runner exited.
        if listener_pid(port):
            return False
        log_path = os.path.join(tempfile.gettempdir(), f"faustus-model-reload-{port}-{time.time_ns()}.log")
        with open(log_path, "ab") as log:
            kwargs = {"cwd": spec["cwd"], "env": spec["env"], "stdin": subprocess.DEVNULL,
                      "stdout": log, "stderr": log}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(spec["argv"], **kwargs)
        logger.info("[model-heal] runner reload log: %s", log_path)
        return True
    except Exception as exc:
        logger.warning("[model-heal] could not reload live llama-server: %s", type(exc).__name__)
        return False


async def _wait_back(url: str, model: str, port: int, timeout_s: float) -> bool:
    """The server listens again, says it is healthy and answers correctly."""
    from src import engine_swap
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if listener_pid(port) and await _health_ok(url):
            sane = await engine_swap.generates_sanely(url, model, timeout_s=60.0)
            if sane is True:
                return True
            if sane is False:
                return False
        await asyncio.sleep(3.0)
    return False


async def _end_process(pid: int) -> bool:
    psutil = _psutil()
    if psutil is None:
        return False
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, 15)
        except psutil.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait, 15)
        return True
    except psutil.NoSuchProcess:
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[model-heal] could not end PID %s: %s", pid, exc)
        return False


def _run_command(command: str) -> bool:
    try:
        kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL, "shell": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(command, **kwargs)  # noqa: S602 - the admin's own configured command
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[model-heal] restart command failed to start: %s", exc)
        return False


async def _heal_once(url: str, model: str, reason: str) -> Dict[str, Any]:
    from src import engine_swap
    cfg = _settings()
    hp = _host_port(url)
    base = {"url": _base(url), "model": model, "reason": reason}
    if not cfg["enabled"]:
        return {**base, "action": "off", "detail": "model_server_auto_heal is off"}
    if hp is None:
        return {**base, "action": "skipped", "detail": "not a server on this machine"}
    port = hp[1]
    idle = await _server_idle(url)
    if idle is False:
        return {**base, "action": "deferred", "detail": "server is processing a request"}
    sane = await engine_swap.generates_sanely(url, model)
    if sane is not False:
        return {**base, "action": "healthy" if sane else "unknown",
                "detail": "answers a simple probe correctly" if sane else "the check got no answer"}
    last = _LAST_RESTART.get(f"{port}")
    if last and time.time() - last < _COOLDOWN_S:
        return _record({**base, "action": "failed",
                        "detail": f"still broken {time.time() - last:.0f}s after a restart; not restarting again yet"})

    engine = engine_swap.restartable_engine_for_url(url)
    command = command_for(port, cfg["commands"])
    pid = listener_pid(port)
    supervisor = supervisor_of(pid) if pid else None
    live = await asyncio.to_thread(_llama_snapshot, pid, port, model) if pid and not supervisor else None
    if engine is None and not command and not supervisor and live is None:
        return _record({**base, "action": "manual", "pid": pid,
                        "detail": ("broken, but nothing here can restart it: it is not a managed engine, "
                                   "has no accessible live llama-server command or restart command, "
                                   "and was not started by a launcher that is still running")})

    if not _take_lock(port, cfg["timeout_s"] + 60):
        # Another Faustus instance is restarting it: wait for that instead.
        back = await _wait_back(url, model, port, cfg["timeout_s"])
        return _record({**base, "action": "waited" if back else "failed",
                        "detail": "another Faustus instance restarted it" if back
                        else "another instance was restarting it and it did not come back sane"})
    try:
        # Generation can take time; another chat may have arrived meanwhile.
        idle = await _server_idle(url)
        if idle is False or (live is not None and idle is not True):
            return {**base, "action": "deferred", "detail": "waiting for verified idle slots before reloading"}
        _LAST_RESTART[f"{port}"] = time.time()
        if supervisor is not None and pid:
            # The live server's own launcher is still running and starts it again with the command it was started
            # with. A managed engine registered for the same port may describe another model: live 01-10 a q4
            # server under its watchdog was "restarted" through the managed q8 engine, and both loaded at once.
            how = f"ended PID {pid}; its launcher {supervisor['name']} (PID {supervisor['pid']}) starts it again"
            started = await _end_process(int(pid))
            back = started and await _wait_back(url, model, port, cfg["timeout_s"])
        elif live is not None:
            how = "live llama-server command, preserving its model and flags"
            started = await _restart_live_llama(live, port)
            back = started and await _wait_back(url, model, port, cfg["timeout_s"])
        elif engine is not None:
            how = f"managed engine {engine.get('id')}"
            ok = await engine_swap.restart_managed_engine(engine)
            back = ok and bool(await engine_swap.generates_sanely(url, model))
        else:
            how = "restart command"
            started = await asyncio.to_thread(_run_command, command)
            back = started and await _wait_back(url, model, port, cfg["timeout_s"])
        return _record({**base, "action": "restarted" if back else "failed", "how": how,
                        "detail": "answers sanely again" if back else "did not come back answering sanely"})
    finally:
        _drop_lock(port)


async def heal(url: str, model: str, *, reason: str = "") -> Dict[str, Any]:
    """Check the server behind ``url`` and restart it if it is stuck on
    garbage. Concurrent calls for one port share the same attempt. The
    result's ``action``: healthy, unknown, restarted, waited, manual,
    failed, deferred, skipped or off."""
    hp = _host_port(url)
    key = f"{hp[1]}" if hp else str(url)
    running = _INFLIGHT.get(key)
    if running is not None and not running.done():
        return await asyncio.shield(running)
    task = asyncio.ensure_future(_heal_once(url, model, reason))
    _INFLIGHT[key] = task
    try:
        return await asyncio.shield(task)
    except Exception as exc:  # noqa: BLE001
        return {"url": _base(url), "model": model, "action": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        if task.done() and _INFLIGHT.get(key) is task:
            _INFLIGHT.pop(key, None)


def schedule(url: str, model: str, reason: str = "") -> bool:
    """Start :func:`heal` in the background (from a place that cannot wait
    for it). False when there is no running event loop or it is not a local
    server. Never raises."""
    if _host_port(url) is None or not _settings()["enabled"]:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    try:
        loop.create_task(heal(url, model, reason=reason))
        return True
    except Exception:  # noqa: BLE001
        return False


def status() -> Dict[str, Any]:
    cfg = _settings()
    return {"enabled": cfg["enabled"], "restart_commands": cfg["commands"],
            "timeout_s": cfg["timeout_s"], "cooldown_s": _COOLDOWN_S,
            "monitor_running": bool(_MONITOR_TASK and not _MONITOR_TASK.done()),
            "check_interval_s": _CHECK_INTERVAL_S,
            "in_progress": sorted(k for k, t in _INFLIGHT.items() if not t.done()),
            "recent": list(_EVENTS)}


async def _monitor_once() -> None:
    if not _settings()["enabled"]:
        return
    from src.endpoint_resolver import resolve_endpoint
    url, model, _ = await asyncio.to_thread(resolve_endpoint, "default")
    if not url or not model or _host_port(url) is None:
        return
    # Do not send a generation into an idle slot that still retains a chat
    # prefix: llama-server may evict it and force a costly re-prefill. Unknown
    # prompt-token state is closed by default. Symptom-triggered heal() calls
    # still run their own confirmation probes.
    slots = await _server_slots(url)
    if slots is None or not _slots_have_empty_prefix(slots):
        return
    await heal(url, model, reason="periodic idle generation check")


async def _monitor_loop() -> None:
    while True:
        await asyncio.sleep(_CHECK_INTERVAL_S)
        try:
            await _monitor_once()
        except Exception as exc:
            logger.debug("[model-heal] idle check failed: %s", type(exc).__name__)


def start() -> None:
    global _MONITOR_TASK
    if _MONITOR_TASK is None or _MONITOR_TASK.done():
        _MONITOR_TASK = asyncio.get_running_loop().create_task(_monitor_loop(), name="model-generation-health")


async def stop() -> None:
    global _MONITOR_TASK
    if _MONITOR_TASK is not None:
        _MONITOR_TASK.cancel()
        try:
            await _MONITOR_TASK
        except asyncio.CancelledError:
            pass
        _MONITOR_TASK = None
    # heal() shields shared attempts from individual callers. At application
    # shutdown they must stop too, rather than relaunch a model after Stop All.
    pending = [task for task in _INFLIGHT.values() if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _INFLIGHT.clear()
