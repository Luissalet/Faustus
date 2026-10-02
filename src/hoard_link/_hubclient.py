"""Finding (and, when allowed, starting) the local Hoard Hub.

Shared by the lease client (:mod:`hoard_link.lease`) and the hub's stdio
MCP bridge, so both locate and auto-start the hub the same way. Standard
library only: this runs inside apps that vendor ``hoard_link/`` and may not
have the hub's optional dependencies.

Where the hub is, in order:

1. an explicit URL from the caller;
2. ``HOARD_HUB_URL``;
3. the ``url`` file the hub writes in its data folder:
   ``HOARD_HUB_DATA_DIR/url``, else ``<hub repo>/data/url``;
4. ``http://127.0.0.1:8810``.

The hub repository is the checkout this package lives in when it carries
``hoard_link/hub/`` (a HoardLink clone), else a sibling ``HoardLink``
folder next to the app that vendors the library, else ``HOARD_LINK_DIR``.
Auto-start runs ``python -m hoard_link.hub --no-window`` from there,
detached and without a console window; ``HOARD_HUB_AUTOSTART=0`` turns it
off.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

DEFAULT_URL = "http://127.0.0.1:8810"
SERVICE = "hoard-hub"
AUTOSTART_WAIT_S = 20.0
#: After a failed auto-start, do not try again for this long (an app that
#: asks for a lease every few seconds must not spawn a hub every time).
AUTOSTART_RETRY_S = 60.0

_PKG_DIR = Path(__file__).resolve().parent
_last_failed: dict[str, float] = {}
_lock = threading.Lock()


def _is_hub_repo(path: Path) -> bool:
    return (path / "hoard_link" / "hub" / "__main__.py").is_file()


def hub_repo_dir() -> Optional[Path]:
    env = os.environ.get("HOARD_LINK_DIR")
    if env and _is_hub_repo(Path(env)):
        return Path(env).resolve()
    here = _PKG_DIR.parent
    if _is_hub_repo(here):
        return here
    for name in ("HoardLink", "Hoard Link", "hoard-link", "Hoard Link's Hoard"):
        cand = here.parent / name
        if _is_hub_repo(cand):
            return cand.resolve()
    return None


def _read_url_file(folder: Path) -> Optional[str]:
    try:
        text = (folder / "url").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return text.rstrip("/") if text.startswith("http") else None


def hub_url(explicit: Optional[str] = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    env = (os.environ.get("HOARD_HUB_URL") or "").strip()
    if env:
        return env.rstrip("/")
    data_dir = (os.environ.get("HOARD_HUB_DATA_DIR") or "").strip()
    if data_dir:
        found = _read_url_file(Path(data_dir))
        if found:
            return found
    repo = hub_repo_dir()
    if repo is not None:
        found = _read_url_file(repo / "data")
        if found:
            return found
    return DEFAULT_URL


def fetch(url: str, body: Optional[dict[str, Any]] = None, *, method: Optional[str] = None,
          timeout: float = 5.0, headers: Optional[dict[str, str]] = None) -> tuple[Optional[int], Any]:
    """``(status, json)``; ``(None, None)`` when nothing answered. Never uses
    a proxy: this is loopback traffic."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "hoard-link", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception:  # noqa: BLE001
        return None, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None
    except ValueError:
        return status, None


def fetch_detailed(url: str, body: Optional[dict[str, Any]] = None, *, method: Optional[str] = None,
                   timeout: float = 5.0, headers: Optional[dict[str, str]] = None) -> tuple[Optional[int], Any, str]:
    """Like :func:`fetch` but also says why nothing answered: ``(status, json, "")`` when something did,
    else ``(None, None, "timeout" | "unreachable")``."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "hoard-link", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception as exc:  # noqa: BLE001
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()
        return None, None, "timeout" if timed_out else "unreachable"
    try:
        return status, json.loads(raw.decode("utf-8", "replace")) if raw else None, ""
    except ValueError:
        return status, None, ""


def is_hub(body: Any) -> bool:
    return isinstance(body, dict) and body.get("service") == SERVICE


def hub_up(url: str, timeout: float = 1.5) -> bool:
    status, body = fetch(url.rstrip("/") + "/api/health", timeout=timeout)
    return status == 200 and is_hub(body)


def autostart_enabled() -> bool:
    return os.environ.get("HOARD_HUB_AUTOSTART", "1").strip().lower() not in ("0", "false", "no", "off")


def start_headless(url: str = DEFAULT_URL, repo: Optional[Path] = None) -> bool:
    """Spawn a detached ``python -m hoard_link.hub --no-window``. Returns
    whether a process was spawned (not whether it came up)."""
    repo = repo or hub_repo_dir()
    env = dict(os.environ)
    cwd: Optional[str] = None
    if repo is not None:
        cwd = str(repo)
        env["PYTHONPATH"] = str(repo) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    else:
        try:
            import hoard_link.hub  # noqa: F401  (importable from where we are?)
        except Exception:  # noqa: BLE001
            return False
    cmd = [sys.executable, "-m", "hoard_link.hub", "--no-window"]
    port = urlsplit(url).port
    if port and port != urlsplit(DEFAULT_URL).port:
        cmd += ["--port", str(port)]
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs: dict[str, Any] = {"creationflags": flags}
    else:
        kwargs = {"start_new_session": True}
    try:
        subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
    except Exception:  # noqa: BLE001
        return False
    return True


def ensure_hub(url: Optional[str] = None, wait_s: float = AUTOSTART_WAIT_S) -> bool:
    """True when a hub answers at ``url`` — starting one headless first
    when none does and auto-start is allowed."""
    url = hub_url(url)
    if hub_up(url):
        return True
    if not autostart_enabled():
        return False
    with _lock:
        if time.monotonic() - _last_failed.get(url, -1e9) < AUTOSTART_RETRY_S:
            return False
        if hub_up(url):
            return True
        if not start_headless(url):
            _last_failed[url] = time.monotonic()
            return False
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if hub_up(url):
                return True
            time.sleep(0.5)
        _last_failed[url] = time.monotonic()
        return False
