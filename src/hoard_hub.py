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


def hub_bearer_matches(authorization: str) -> bool:
    """True when ``authorization`` is exactly ``Bearer <the Hub's own token>``.

    The Hub reads published metadata with the credential it already shares with the family. It is not an
    ``ody_`` API token, so the session middleware cannot recognise it; the one route that accepts it
    (``GET /api/family/fleet``) asks this function, in the middleware and again in the handler.
    """
    import secrets

    token = _read_text(hub_location().get("token_file") or "")
    if not token or not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return False
    return secrets.compare_digest(authorization.encode(), ("Bearer " + token).encode())


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


def _app_key(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def start_app_named(name: str, *, timeout: float = 90.0) -> bool:
    """Start the family app called ``name`` (the MCP server's display name,
    e.g. «Ledger's Hoard») through the Hub and wait until it answers.

    True when the Hub reports it running. Used when a Hoard tool answers
    "not running": the agent should open the app, not ask the user to.
    """
    key = _app_key(name)
    if not key:
        return False
    status, body, _ = _call("GET", "/api/apps")
    apps = (body or {}).get("apps") if isinstance(body, dict) else None
    if status != 200 or not isinstance(apps, list):
        return False
    match = None
    for app in apps:
        if not isinstance(app, dict):
            continue
        names = {_app_key(app.get("name")), _app_key(app.get("id")),
                 _app_key(str(app.get("name") or "").replace("'s", ""))}
        if key in names or key.replace("hoard", "") in {n.replace("hoard", "") for n in names if n}:
            match = app
            break
    if not match or not match.get("id"):
        return False
    status, body, _ = _call("POST", f"/api/apps/{match['id']}/start", {}, timeout=timeout)
    if status is None or status >= 400:
        return False
    if isinstance(body, dict) and body.get("ok") is False:
        return False
    return True


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


def get_fleet() -> Dict[str, Any]:
    """Read the Hub's published fleet metadata without opening another app's data."""
    # The Hub answers from its background cache (marking it ``refreshing`` while a pass runs); the deadline
    # only covers a Hub that is slow to start.
    status, data, where = _call("GET", "/api/fleet", timeout=15.0)
    if status != 200 or not isinstance(data, dict) or not data.get("ok"):
        return {"ok": False, "error": "fleet unavailable", "hub_url": where["url"]}
    if not isinstance(data.get("items"), list) or not isinstance(data.get("brief"), dict):
        return {"ok": False, "error": "fleet unavailable", "hub_url": where["url"]}
    return {k: data.get(k) for k in ("ok", "generated_ts", "items", "brief", "counts", "coverage", "refreshing")}


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
