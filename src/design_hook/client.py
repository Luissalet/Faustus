"""The design workshop's HTTP API, reached only when the app is already running.

The workshop (the design-rules Hoard) is a cold host with three modules, one
of them `design`, which holds the rules and the renderer and is mounted under
`/design`. The hook speaks to that module only when the host answers *and* the
module is on. Nothing here starts anything by itself: when the host is stopped
or the module is off every call answers `None` and the design hook stays
silent. `start_design_module` is the one explicit action, called from a button
or a tool, never from an edit. The answer to "is it usable" is cached for a few
seconds so a burst of edits costs one probe, not one each.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:5191"
SERVICE = "vitruvius-hoard"
MODULE = "design"
PROBE_TIMEOUT_S = 0.6
START_TIMEOUT_S = 90.0
LINT_TIMEOUT_S = 8.0
RENDER_TIMEOUT_S = 75.0
ALIVE_TTL_S = 15.0

_LOCK = threading.Lock()
_INFO: Dict[str, Any] = {"url": "", "info": None, "at": 0.0}


def workshop_url() -> str:
    """Where the workshop is expected: the environment, its connector, else the default port."""
    env = os.environ.get("FAUSTUS_VITRUVIUS_URL", "").strip()
    if env:
        return env.rstrip("/")
    try:
        from src import connector_sidecar
        for entry in connector_sidecar.list_connectors():
            if entry.get("preset_id") == "vitruvius":
                url = entry.get("app_url") or (entry.get("values") or {}).get("APP_URL")
                if url:
                    return str(url).rstrip("/")
    except Exception:  # noqa: BLE001 - the default is good enough
        logger.debug("design_hook: connector lookup failed", exc_info=True)
    return DEFAULT_URL


def reset_cache() -> None:
    with _LOCK:
        _INFO.update(url="", info=None, at=0.0)


def _probe(url: str) -> Dict[str, Any]:
    """{running, layout, design, base}: what answers at `url` and where the design rules can be asked."""
    gone = {"running": False, "layout": None, "design": None, "base": None}
    try:
        r = httpx.get(url + "/api/health", timeout=PROBE_TIMEOUT_S)
        data = r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001 - stopped, starting or not the workshop
        return gone
    if not isinstance(data, dict) or data.get("service") != SERVICE:
        return gone
    modules = data.get("modules")
    if isinstance(modules, list):  # the cold host: the rules live in its design module
        row = next((m for m in modules if isinstance(m, dict) and m.get("module") == MODULE), {})
        state = str(row.get("state") or "off")
        return {"running": True, "layout": "host", "design": state, "base": f"{url}/{MODULE}" if state == "on" else None}
    return {"running": True, "layout": "app", "design": "on", "base": url}  # a workshop that serves the rules itself


def info(*, force: bool = False) -> Dict[str, Any]:
    """The probe, cached for ALIVE_TTL_S."""
    url = workshop_url()
    now = time.monotonic()
    with _LOCK:
        if not force and _INFO["url"] == url and _INFO["info"] is not None and now - _INFO["at"] < ALIVE_TTL_S:
            return dict(_INFO["info"])
    found = _probe(url)
    with _LOCK:
        _INFO.update(url=url, info=found, at=time.monotonic())
    return dict(found)


def base_if_running(*, force: bool = False) -> Optional[str]:
    """The base URL of the design rules when the workshop answers and its design module is on, else None."""
    return info(force=force).get("base")


def start_design_module() -> Dict[str, Any]:
    """Turn the design module on (an explicit action). Returns the fresh probe plus `started`/`error`."""
    url = workshop_url()
    now = info(force=True)
    if not now["running"]:
        return {**now, "started": False, "error": "workshop_stopped"}
    if now["design"] == "on":
        return {**now, "started": False}
    try:
        r = httpx.post(url + "/api/ui/call", json={"name": "software_module_set",
                                                    "arguments": {"module": MODULE, "enabled": True, "automatic": False}},
                       timeout=START_TIMEOUT_S)
        error = None if r.status_code < 400 else f"http_{r.status_code}"
    except Exception as exc:  # noqa: BLE001
        error = type(exc).__name__
    after = info(force=True)
    out = {**after, "started": after["design"] == "on"}
    if error and not out["started"]:
        out["error"] = error
    return out


def _post(base: str, path: str, body: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
    try:
        r = httpx.post(base + path, json=body, timeout=timeout)
        if r.status_code >= 400:
            logger.debug("design_hook: %s answered %s: %s", path, r.status_code, r.text[:200])
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        logger.debug("design_hook: %s failed", path, exc_info=True)
        return None


def lint_static(base: str, html: str) -> Optional[List[Dict[str, Any]]]:
    """Findings of the static rules over `html`, with no browser; None when the call failed."""
    data = _post(base, "/api/lint", {"html": html, "engines": ["static"]}, LINT_TIMEOUT_S)
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return None
    return data["findings"]


def render_page(base: str, html: str, *, title: str = "") -> Optional[Dict[str, Any]]:
    """Render a page (390 and 1440 wide, no vision) and keep it; None when the render failed."""
    row = _post(base, "/api/renders", {"html": html, "widths": [390, 1440], "full_page": False, "wait_ms": 300,
                                       "lint": False, "title": title[:120]}, RENDER_TIMEOUT_S)
    if not isinstance(row, dict) or not row.get("id"):
        return None
    if isinstance((row.get("metrics") or {}).get("error"), str):
        return {"id": row["id"], "error": row["metrics"]["error"]}
    return {"id": row["id"]}


def critique_render(base: str, render_id: str, waivers: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """The tier gate and every finding of a stored render (deterministic rules only)."""
    return _post(base, f"/api/renders/{render_id}/critique", {"use_vision": False, "waivers": waivers}, RENDER_TIMEOUT_S)
