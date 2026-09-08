"""Bring the self-hosted search backend up before anything asks it to search.

08-09-2026: a Deep Research ran for four minutes, wrote seven queries and read
nothing. SearXNG was not running (its container was down because Docker itself
was not started) and the DuckDuckGo fallback, rate-limited, answered zero. The
run failed on a service the machine was perfectly capable of starting.

So it starts it. This is the same rule the model load follows: a dependency
that can be prepared is prepared, with the wait shown, instead of being raced
and reported as a failure. Only local, self-hosted backends defined in this
repo's own compose file are touched — never someone else's server, never a
service this repo does not ship.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMPOSE_FILE = os.path.join(ROOT, "docker-compose.yml")

# Providers this can start, and the compose service that serves each one.
# Firecrawl is deliberately absent: this repo's compose does not ship it, and
# guessing at an image would start something the admin never chose.
_SERVICE = {"searxng": "searxng"}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}

# Where Docker Desktop lives when the engine is installed but not running.
_DESKTOP_APP = {
    "win32": r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
    "darwin": "/Applications/Docker.app",
}


def provider_url(provider: str) -> str:
    """The URL a startable provider is expected to answer on."""
    from .providers import _get_firecrawl_instance, _get_search_instance
    if provider == "searxng":
        return _get_search_instance()
    if provider == "firecrawl":
        return _get_firecrawl_instance()
    return ""


def _is_local(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


def _settings() -> dict:
    try:
        from src.settings import load_settings
        return load_settings() or {}
    except Exception:
        return {}


def _bounded(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))


async def _reachable(url: str, timeout: float = 3.0) -> bool:
    """Does something answer HTTP there? Any non-5xx counts: SearXNG serves its
    search page on / and that is proof enough that the appliance is up."""
    if not url:
        return False
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url)
            return r.status_code < 500
    except Exception:
        return False


async def _run(*argv: str, timeout: float = 120.0) -> Tuple[int, str]:
    """Run a fixed argv (never a shell string, never user input) and collect it."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=ROOT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace").strip()


async def _engine_up() -> bool:
    code, _ = await _run("docker", "version", "--format", "{{.Server.Version}}", timeout=20)
    return code == 0


async def _start_engine(deadline: float, say: Callable[[str], None]) -> bool:
    """Start Docker Desktop when the CLI is there but the engine is not.

    On Linux the daemon is the host's business (systemd, rootless, a remote
    context): we do not touch it, we just report.
    """
    app = _DESKTOP_APP.get(sys.platform)
    if not app or not os.path.exists(app):
        return False
    say("Starting Docker…")
    logger.info("Search appliance: starting Docker Desktop")
    try:
        if sys.platform == "darwin":
            await _run("open", "-a", app, timeout=30)
        else:
            # Detached: Docker Desktop outlives this call by design.
            await asyncio.create_subprocess_exec(
                app, "-Autostart",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
    except Exception as e:
        logger.warning(f"Search appliance: could not launch Docker Desktop: {e}")
        return False
    while time.time() < deadline:
        if await _engine_up():
            logger.info("Search appliance: Docker engine is up")
            return True
        await asyncio.sleep(4)
    return False


async def ensure_backend(provider: str, *, on_progress=None,
                         timeout: Optional[int] = None) -> Tuple[bool, str]:
    """Make sure `provider` can answer, starting it if this repo ships it.

    Returns (ready, reason). `ready` False is never fatal on its own — the
    caller still has the fallback chain — but the reason is worth showing.
    """
    def say(message: str) -> None:
        if on_progress:
            try:
                on_progress({"phase": "starting_search", "message": message})
            except Exception:
                pass

    url = provider_url(provider)
    if not url:
        return True, ""  # A hosted provider with a key: nothing to start.
    if await _reachable(url):
        return True, ""

    settings = _settings()
    if not settings.get("search_autostart", True):
        return False, f"{provider} is not answering at {url} (autostart is off)"
    if not _is_local(url):
        return False, f"{provider} is not answering at {url}"
    service = _SERVICE.get(provider)
    if not service:
        return False, (f"{provider} is not answering at {url} — it is not one of "
                       "the services this install can start on its own")
    if not os.path.exists(COMPOSE_FILE) or not shutil.which("docker"):
        return False, f"{provider} is not answering at {url} and Docker is not available here"

    budget = _bounded(
        timeout if timeout is not None else settings.get("search_autostart_timeout_seconds", 300),
        default=300, minimum=30, maximum=1800,
    )
    deadline = time.time() + budget
    logger.info(f"Search appliance: {provider} unreachable at {url}; starting '{service}' (budget={budget}s)")

    if not await _engine_up() and not await _start_engine(deadline, say):
        return False, ("Docker is not running, and this machine did not start it "
                       f"in {budget}s — {provider} stays unreachable")

    say(f"Starting the search engine ({service})…")
    code, out = await _run(
        "docker", "compose", "-f", COMPOSE_FILE, "up", "-d", service,
        timeout=max(30, int(deadline - time.time())),
    )
    if code != 0:
        logger.warning(f"Search appliance: compose up failed ({code}): {out[-400:]}")
        return False, f"The search engine did not start: {out.splitlines()[-1] if out else code}"

    say("Waiting for the search engine…")
    while time.time() < deadline:
        if await _reachable(url):
            logger.info(f"Search appliance: {provider} is up at {url}")
            return True, ""
        await asyncio.sleep(3)
    return False, f"The search engine started but did not answer at {url} within {budget}s"
