"""hoard_hub.py — Faustus's thin client for the family hub's spheres.

The hub ("Hoard Hub", default http://127.0.0.1:8810) keeps the person's two
lives apart as *spheres* (personal / work, optionally more). Faustus does not
own that state; it shows the active sphere in the Studio header, lets the
person switch it, and tells the agent which one is active.

Everything here is best-effort and loopback-only:

* The browser never calls the hub (cross-origin); `routes/hoard_hub_routes.py`
  forwards through this module.
* A hub that is down, slow or refusing never raises and never becomes an HTTP
  error: the caller gets ``{"ok": False, "error": "hub unreachable"}`` (or
  ``"hub refused"``) and the UI hides the chip.
* The prompt path (`context_line`) never touches the network. It reads the
  last answer the hub gave (kept for `CONTEXT_TTL_S`), and when that answer is
  stale and a hub is configured it asks for a refresh on a daemon thread, so a
  turn is never delayed by a hub that is not there.

Where the hub lives, in order: the ``hoardhub`` connector's app URL (and its
TOKEN_FILE, or ``<HOARDLINK_DIR>/data/mcp-token``), then ``HOARD_HUB_URL`` /
``HOARD_HUB_TOKEN_FILE``, then the ``url`` file the hub writes in its data
folder (``HOARD_HUB_DATA_DIR`` or ``<HOARDLINK_DIR>/data``), then
``http://127.0.0.1:8810``. Without a token the hub's read route still works
(it only needs loopback) and a switch is refused with 401, reported as
``"hub refused"``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8810"
TIMEOUT_S = 3.0
PRESET_ID = "hoardhub"

#: How long the last answer stays good for the agent's context line, and how
#: long a failed refresh is not retried.
CONTEXT_TTL_S = 300.0
RETRY_AFTER_FAIL_S = 60.0

_UNREACHABLE = {"ok": False, "error": "hub unreachable"}
_REFUSED = {"ok": False, "error": "hub refused"}

_lock = threading.Lock()
_last: Dict[str, Any] = {"ts": 0.0, "id": "", "name": ""}
_refresh_state: Dict[str, Any] = {"running": False, "last_try": 0.0}

# A loopback hub is never behind a proxy, whatever the environment says.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ── where the hub is ────────────────────────────────────────────────────────

def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _connector_values() -> Optional[Dict[str, Any]]:
    """The saved hoardhub connector (url + values), or None."""
    try:
        from src import connector_sidecar

        for row in connector_sidecar.list_connectors(redact=True) or []:
            if row.get("preset_id") == PRESET_ID:
                return row
    except Exception:  # noqa: BLE001 - no sidecar means no connector
        logger.debug("hoard_hub: cannot read connectors", exc_info=True)
    return None


def hub_location() -> Dict[str, Any]:
    """``{"url", "token_file", "configured"}`` for the hub, never raising.

    `configured` is True when the person (or the environment) told Faustus
    about a hub; it is what allows the background refresh behind the agent's
    context line. The bare default URL is still tried by the Studio chip.
    """
    url = ""
    token_file = ""
    hub_dir = ""
    configured = False

    row = _connector_values()
    if row:
        configured = True
        values = row.get("values") or {}
        url = str(row.get("app_url") or "").strip()
        token_file = str(values.get("TOKEN_FILE") or "").strip()
        hub_dir = str(values.get("HOARDLINK_DIR") or "").strip()

    env_url = (os.environ.get("HOARD_HUB_URL") or "").strip()
    if not url and env_url:
        url = env_url
        configured = True
    if not token_file:
        token_file = (os.environ.get("HOARD_HUB_TOKEN_FILE") or "").strip()
        if token_file:
            configured = True
    if not hub_dir:
        hub_dir = (os.environ.get("HOARDLINK_DIR") or "").strip()

    data_dir = (os.environ.get("HOARD_HUB_DATA_DIR") or "").strip()
    if not data_dir and hub_dir:
        data_dir = os.path.join(hub_dir, "data")
    if not url and data_dir:
        found = _read_text(os.path.join(data_dir, "url"))
        if found.startswith("http"):
            url = found
            configured = True
    if not token_file and data_dir:
        candidate = os.path.join(data_dir, "mcp-token")
        if os.path.isfile(candidate):
            token_file = candidate

    return {
        "url": (url or DEFAULT_URL).rstrip("/"),
        "token_file": token_file,
        "configured": configured,
    }


# ── talking to it ───────────────────────────────────────────────────────────

def _call(method: str, path: str, body: Optional[dict] = None, *,
          timeout: float = TIMEOUT_S) -> Tuple[Optional[int], Any, Dict[str, Any]]:
    """``(status, json, location)``; status None when nothing answered."""
    where = hub_location()
    headers = {"Accept": "application/json"}
    token = _read_text(where["token_file"]) if where["token_file"] else ""
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(where["url"] + path, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read(1_000_000)
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(1_000_000)
        except Exception:  # noqa: BLE001
            raw = b""
        status = exc.code
    except Exception:  # noqa: BLE001 - refused, timed out, bad URL: all "unreachable"
        return None, None, where
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return status, parsed, where


def _slim(spheres: Any) -> List[Dict[str, Any]]:
    """Only what the chip needs: the hub's mail rules and VIP lists stay there."""
    out: List[Dict[str, Any]] = []
    for item in spheres if isinstance(spheres, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        name = item.get("name")
        if isinstance(name, str):
            name = {"es": name, "en": name}
        elif not isinstance(name, dict):
            name = {}
        out.append({
            "id": str(item["id"]),
            "name": {"es": str(name.get("es") or name.get("en") or item["id"]),
                     "en": str(name.get("en") or name.get("es") or item["id"])},
            "color": str(item.get("color") or ""),
        })
    return out


def _remember(active: str, spheres: List[Dict[str, Any]]) -> None:
    name = active
    for s in spheres:
        if s["id"] == active:
            name = s["name"]["en"] or active
            break
    with _lock:
        _last.update({"ts": time.time(), "id": active, "name": name})


def _classify_failure(status: Optional[int]) -> Dict[str, Any]:
    if status is None:
        return dict(_UNREACHABLE)
    if status in (401, 403):
        return dict(_REFUSED)
    return {"ok": False, "error": f"hub answered {status}", "status": status}


def get_spheres() -> Dict[str, Any]:
    """``{ok, active, spheres: [{id, name: {es, en}, color}], hub_url}``."""
    status, data, where = _call("GET", "/api/spheres")
    if status != 200 or not isinstance(data, dict) or data.get("ok") is False:
        with _lock:
            _last["ts"] = 0.0
        return _classify_failure(status if status != 200 else 502)
    spheres = _slim(data.get("spheres"))
    active = str(data.get("active") or "")
    _remember(active, spheres)
    return {"ok": True, "active": active, "spheres": spheres, "hub_url": where["url"]}


def set_active(sphere_id: str) -> Dict[str, Any]:
    """Switch the hub's active sphere. Same shape as `get_spheres` on success."""
    sid = str(sphere_id or "").strip()
    if not sid:
        return {"ok": False, "error": "id is required", "status": 400}
    status, data, where = _call("POST", "/api/spheres/active", {"id": sid})
    if status != 200 or not isinstance(data, dict) or data.get("ok") is False:
        failure = _classify_failure(status if status != 200 else 502)
        if status == 200 and isinstance(data, dict) and data.get("error"):
            failure = {"ok": False, "error": str(data["error"])[:200]}
        elif status not in (None, 200, 401, 403) and isinstance(data, dict) and data.get("error"):
            failure["error"] = str(data["error"])[:200]
        return failure
    return get_spheres()


# ── the agent's one line ────────────────────────────────────────────────────

def _refresh_in_background() -> None:
    def run() -> None:
        try:
            get_spheres()
        except Exception:  # noqa: BLE001
            logger.debug("hoard_hub: background refresh failed", exc_info=True)
        finally:
            with _lock:
                _refresh_state["running"] = False

    with _lock:
        now = time.time()
        if _refresh_state["running"] or now - _refresh_state["last_try"] < RETRY_AFTER_FAIL_S:
            return
        _refresh_state.update({"running": True, "last_try": now})
    threading.Thread(target=run, name="hoard-hub-sphere", daemon=True).start()


def context_line() -> str:
    """``"Active sphere (Hoard Hub): Work"`` or ``""``; never blocks.

    Only a fresh answer from the hub is used, so a hub that went away stops
    appearing in the prompt within `CONTEXT_TTL_S`.
    """
    with _lock:
        fresh = _last["id"] and time.time() - _last["ts"] < CONTEXT_TTL_S
        name = _last["name"]
    if fresh:
        return f"Active sphere (Hoard Hub): {name}"
    try:
        if hub_location()["configured"]:
            _refresh_in_background()
    except Exception:  # noqa: BLE001
        logger.debug("hoard_hub: context refresh skipped", exc_info=True)
    return ""


def _reset_for_tests() -> None:
    with _lock:
        _last.update({"ts": 0.0, "id": "", "name": ""})
        _refresh_state.update({"running": False, "last_try": 0.0})
