"""sparks.py — the DGX Spark cluster as Faustus's model backend, through Prometheus's Hoard.

Prometheus's Hoard (default http://127.0.0.1:5205) runs the Sparks: it knows which recipes are loaded and which OpenAI-compatible
endpoints they serve. Faustus does not talk to the Sparks itself. This module:

* reads Prometheus (``/api/overview`` for the live figures, ``/api/endpoints`` for the servers, ``/api/ui/call`` for loading and
  unloading recipes), always best-effort: a Prometheus that is closed is ``{"ok": False, "error": "prometheus unreachable"}``, never
  an exception or an HTTP error, and the header pill hides itself;
* keeps one Faustus endpoint per recipe (``Sparks · <title>``), enabled while that recipe serves and disabled when it does not, so the
  model picker offers exactly what the Sparks can answer now;
* when ``sparks_default_backend`` is on, makes the Sparks the default chat backend while a recipe serves (the preferred recipe in
  ``sparks_recipe``, else the one Prometheus marks as default, else the first one running) and gives the default back to the local
  model the person had (remembered in ``sparks_local_default``) when the Sparks stop serving. Choosing another default by hand while
  the Sparks serve turns ``sparks_default_backend`` off, so the two never fight.

Every value is a setting, editable in Settings → Sparks: nothing about the cluster is fixed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:5205"
TIMEOUT_S = 4.0
SYNC_INTERVAL_S = 20.0
ENDPOINT_PREFIX = "sparks-"

_UNREACHABLE = {"ok": False, "error": "prometheus unreachable"}
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_lock = threading.RLock()
_last_status: Dict[str, Any] = {"ts": 0.0, "data": None}


# ── settings ────────────────────────────────────────────────────────────────

def config() -> Dict[str, Any]:
    from src.settings import load_settings

    s = load_settings()
    url = str(s.get("sparks_url") or DEFAULT_URL).strip().rstrip("/") or DEFAULT_URL
    return {
        "enabled": bool(s.get("sparks_enabled", True)),
        "url": url,
        "default_backend": bool(s.get("sparks_default_backend", True)),
        "recipe": str(s.get("sparks_recipe") or "").strip(),
        "first_token_timeout_s": first_token_timeout(s),
        "endpoints": dict(s.get("sparks_endpoints") or {}),
        "local_default": dict(s.get("sparks_local_default") or {}),
        "applied": dict(s.get("sparks_applied_default") or {}),
    }


def first_token_timeout(settings: Dict[str, Any]) -> float:
    try:
        value = float(settings.get("sparks_first_token_timeout_s", 30))
        return min(120.0, max(5.0, value)) if value == value else 30.0
    except (TypeError, ValueError):
        return 30.0


def _update(patch: Dict[str, Any]) -> None:
    from src.settings import update_settings

    update_settings(patch)


# ── HTTP to Prometheus ─────────────────────────────────────────────────────

def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None, *, timeout: float = TIMEOUT_S,
             url: Optional[str] = None) -> Tuple[Optional[int], Any]:
    base = url or config()["url"]
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data else {})})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "null")
        except Exception:  # noqa: BLE001
            return exc.code, {"error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - closed, refused, timeout, bad JSON
        logger.debug("sparks: %s %s failed: %s", method, path, exc)
        return None, None


def _call(name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    code, body = _request("POST", "/api/ui/call", {"name": name, "arguments": arguments}, timeout=timeout)
    if code is None:
        return dict(_UNREACHABLE)
    if code != 200:
        out = {"ok": False, "status": code}
        if isinstance(body, dict):
            out.update({k: body[k] for k in ("error", "code", "hint", "conflicts") if k in body})
        return out
    return {"ok": True, "result": body}


# ── status for the pill and the settings screen ────────────────────────────

def _trim_node(n: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("id", "name", "online", "error", "power_state", "hostname", "uptime_s", "deployments")
    out = {k: n.get(k) for k in keep}
    mem = n.get("memory") or {}
    gpu = n.get("gpu") or {}
    cpu = n.get("cpu") or {}
    out["memory"] = {"total": mem.get("total"), "used": mem.get("used"), "percent": mem.get("percent")}
    out["gpu"] = {"util": gpu.get("util"), "temp_c": gpu.get("temp_c"), "power_w": gpu.get("power_w"), "clock_mhz": gpu.get("clock_mhz")}
    out["cpu"] = {"percent": cpu.get("percent"), "cores": cpu.get("cores")}
    fabric = n.get("fabric") or {}
    out["fabric"] = {"up": sum(1 for v in fabric.values() if v.get("up")), "total": len(fabric),
                     "rx_bps": sum((v.get("rx_bps") or 0) for v in fabric.values()), "tx_bps": sum((v.get("tx_bps") or 0) for v in fabric.values())}
    return out


def _detected_nodes(server: Dict[str, Any]) -> List[str]:
    """Keep the controller's worker residency; older controllers report only a head."""
    nodes = server.get("nodes")
    if not isinstance(nodes, list):
        nodes = []
    reported = list(dict.fromkeys(n for n in nodes if isinstance(n, str) and n.strip()))
    head = server.get("node")
    return reported or ([head] if isinstance(head, str) and head.strip() else [])


def status(*, with_recipes: bool = True) -> Dict[str, Any]:
    cfg = config()
    base = {"enabled": cfg["enabled"], "url": cfg["url"], "default_backend": cfg["default_backend"], "recipe": cfg["recipe"],
            "first_token_timeout_s": cfg["first_token_timeout_s"],
            "local_default": cfg["local_default"]}
    if not cfg["enabled"]:
        return {"ok": False, "error": "disabled", **base}
    code, overview = _request("GET", "/api/overview")
    if code != 200 or not isinstance(overview, dict):
        return {**_UNREACHABLE, **base}
    out: Dict[str, Any] = {"ok": True, **base,
                           "nodes": [_trim_node(n) for n in overview.get("nodes", [])],
                           "cluster": overview.get("cluster", {}),
                           "jobs": [{k: j.get(k) for k in ("id", "kind", "title", "state", "progress", "node")} for j in overview.get("jobs", [])],
                           "deployments": [{k: d.get(k) for k in ("recipe", "title", "state", "step", "message", "nodes", "head", "base_url",
                                                                   "served", "served_model_name", "max_model_len", "external", "context_verified")}
                                           for d in overview.get("deployments", [])]
                           + [{"recipe": x.get("recipe"), "title": x.get("title"), "state": "running" if x.get("up") else "starting",
                               "nodes": _detected_nodes(x), "head": x.get("node"), "base_url": x.get("base_url"), "served": x.get("models") or [],
                               "max_model_len": x.get("max_model_len"), "detected": True}
                              for x in overview.get("detected", []) if isinstance(x, dict)]}
    if with_recipes:
        r = _call("recipes_list", {}, timeout=10)
        if r.get("ok"):
            out["recipes"] = [{k: x.get(k) for k in ("name", "title", "description", "nodes", "head", "max_model_len", "memory_gb",
                                                     "served_model_name", "tags", "invalid", "validation_status", "context_verified")}
                              for x in r["result"].get("recipes", [])]
    out["effective"] = effective_default()
    out["endpoint_ids"] = cfg["endpoints"]
    with _lock:
        _last_status.update(ts=time.time(), data=out)
    return out


def effective_default() -> Dict[str, Any]:
    from src.settings import load_settings

    s = load_settings()
    ep = str(s.get("default_endpoint_id") or "")
    managed = set((s.get("sparks_endpoints") or {}).values())
    return {"endpoint_id": ep, "model": str(s.get("default_model") or ""), "on_sparks": ep in managed and bool(ep)}


# ── endpoints and the default ──────────────────────────────────────────────

def _endpoints_from_prometheus() -> Optional[List[Dict[str, Any]]]:
    code, body = _request("GET", "/api/endpoints")
    if code != 200 or not isinstance(body, dict):
        return None
    return [e for e in body.get("endpoints", []) if isinstance(e, dict) and e.get("recipe") and e.get("base_url")]


def _upsert_endpoints(running: List[Dict[str, Any]], recipes: List[Dict[str, Any]], known: Dict[str, str]) -> Dict[str, str]:
    """One ModelEndpoint per recipe seen; enabled while it serves. Returns recipe -> endpoint id."""
    from core.database import ModelEndpoint, SessionLocal

    ids = dict(known)
    serving = {e["recipe"]: e for e in running}
    titles = {r.get("name"): r.get("title") or r.get("name") for r in recipes if r.get("name")}
    titles.update({e["recipe"]: e.get("title") or e["recipe"] for e in running})
    db = SessionLocal()
    try:
        for recipe in sorted(set(titles) | set(ids)):
            ep_id = ids.get(recipe)
            row = db.query(ModelEndpoint).filter(ModelEndpoint.id == ep_id).first() if ep_id else None
            live = serving.get(recipe)
            if row is None and not live:
                continue  # a recipe never loaded does not need an endpoint yet
            if row is None:
                ep_id = f"{ENDPOINT_PREFIX}{uuid.uuid4().hex[:8]}"
                row = ModelEndpoint(id=ep_id, name=f"Sparks · {titles.get(recipe, recipe)}", base_url=live["base_url"], api_key=None,
                                    is_enabled=True, model_type="llm", endpoint_kind="local", model_refresh_mode="manual",
                                    supports_tools=True)
                db.add(row)
                ids[recipe] = ep_id
            row.name = f"Sparks · {titles.get(recipe, recipe)}"
            if live:
                models = [m for m in (live.get("models") or []) if m]
                row.base_url = live["base_url"]
                row.is_enabled = True
                row.cached_models = json.dumps(models)
                row.pinned_models = json.dumps(models)
            else:
                row.is_enabled = False
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("sparks: could not update the endpoints", exc_info=True)
    finally:
        db.close()
    return ids


def _choose(running: List[Dict[str, Any]], preferred: str) -> Optional[Dict[str, Any]]:
    if not running:
        return None
    for e in running:
        if preferred and e["recipe"] == preferred:
            return e
    for e in running:
        if e.get("default"):
            return e
    return running[0]


def _set_default(endpoint_id: str, model: str) -> None:
    """Write the chat default the same way Settings → Default AI does (global, and the per-user prefs that carry one)."""
    from src.settings import load_settings, save_settings

    s = load_settings()
    s["default_endpoint_id"] = endpoint_id
    s["default_model"] = model
    save_settings(s)
    try:
        from routes.prefs_routes import _load, _save_for_user

        users = (_load() or {}).get("_users") or {}
        for user, prefs in users.items():
            if isinstance(prefs, dict) and prefs.get("default_endpoint_id"):
                prefs = dict(prefs)
                prefs["default_endpoint_id"] = endpoint_id
                prefs["default_model"] = model
                _save_for_user(user, prefs)
    except Exception:  # noqa: BLE001 - prefs are optional
        logger.debug("sparks: per-user default not updated", exc_info=True)


def sync() -> Dict[str, Any]:
    """Bring the endpoints and the default in line with what the Sparks serve now."""
    with _lock:
        cfg = config()
        if not cfg["enabled"]:
            # Switched off: nothing of the Sparks may stay in use — disable their endpoints and give the default back.
            _disable_managed(cfg["endpoints"])
            action = _restore_local(cfg) if effective_default()["on_sparks"] else "none"
            return {"ok": False, "error": "disabled", "action": action}
        running = _endpoints_from_prometheus()
        if running is None:
            # Prometheus closed is not the same as the model gone: keep the Sparks default only while its server still answers.
            eff = effective_default()
            if eff["on_sparks"] and not _endpoint_answers(eff["endpoint_id"], eff.get("model") or ""):
                _disable_managed({k: v for k, v in cfg["endpoints"].items() if v == eff["endpoint_id"]})
                return {**_UNREACHABLE, "action": _restore_local(cfg)}
            return {**_UNREACHABLE, "action": "none"}
        rec = _call("recipes_list", {}, timeout=10)
        recipes = rec["result"].get("recipes", []) if rec.get("ok") else []
        ids = _upsert_endpoints(running, recipes, cfg["endpoints"])
        patch: Dict[str, Any] = {}
        if ids != cfg["endpoints"]:
            patch["sparks_endpoints"] = ids
        managed = set(ids.values())
        eff = effective_default()
        applied = cfg["applied"]
        action = "none"
        # Remember the latest local default, so whatever puts the Sparks in its place (the switch or a pick by hand) can give it back.
        if not eff["on_sparks"] and eff["endpoint_id"]:
            here = {"endpoint_id": eff["endpoint_id"], "model": eff["model"]}
            if cfg.get("local_default") != here:
                patch["sparks_local_default"] = here
                cfg["local_default"] = here
        # The person picked another default by hand (local, or another model on the Sparks) after the automatic one: respect it
        # and stop switching.
        if applied.get("endpoint_id") and eff["endpoint_id"] and (eff["endpoint_id"], eff["model"]) != (applied["endpoint_id"], applied.get("model", "")):
            patch["sparks_default_backend"] = False
            patch["sparks_applied_default"] = {}
            cfg["default_backend"] = False
            action = "user_override"
        target = _choose(running, cfg["recipe"]) if cfg["default_backend"] else None
        if target:
            ep_id = ids.get(target["recipe"])
            model = (target.get("models") or [""])[0]
            if ep_id and model and (eff["endpoint_id"] != ep_id or eff["model"] != model):
                if not eff["on_sparks"] and eff["endpoint_id"]:
                    patch["sparks_local_default"] = {"endpoint_id": eff["endpoint_id"], "model": eff["model"]}
                _set_default(ep_id, model)
                patch["sparks_applied_default"] = {"endpoint_id": ep_id, "model": model, "recipe": target["recipe"]}
                action = "to_sparks"
        elif eff["endpoint_id"] in managed and eff["endpoint_id"]:
            # The default is on the Sparks while the automatic switch is off (or nothing serves). Give it back only when it no longer
            # serves, or when it is still the automatic choice being undone; a Sparks model picked by hand stays.
            serving = {ids.get(e["recipe"]): set(e.get("models") or []) for e in running}
            still_serves = eff["model"] in serving.get(eff["endpoint_id"], set())
            automatic = bool(applied.get("endpoint_id")) and action != "user_override"
            if not still_serves or automatic:
                action = _restore_local(cfg)
        if patch:
            _update(patch)
        return {"ok": True, "running": [e["recipe"] for e in running], "endpoints": ids, "action": action,
                "default": effective_default()}


def _restore_local(cfg: Dict[str, Any]) -> str:
    """Give the chat default back to the local model it had before the Sparks (or leave it empty if there was none)."""
    local = cfg.get("local_default") or {}
    _set_default(local.get("endpoint_id", ""), local.get("model", ""))
    _update({"sparks_applied_default": {}})
    return "to_local"


def _disable_managed(ids: Dict[str, str]) -> None:
    if not ids:
        return
    from core.database import ModelEndpoint, SessionLocal

    db = SessionLocal()
    try:
        for row in db.query(ModelEndpoint).filter(ModelEndpoint.id.in_(list(ids.values()))).all():
            row.is_enabled = False
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.debug("sparks: could not disable endpoints", exc_info=True)
    finally:
        db.close()


def _endpoint_answers(endpoint_id: str, model: str = "") -> bool:
    """The Sparks endpoint still serves the default model: an answer on the port is not enough, another server may hold it now."""
    from core.database import ModelEndpoint, SessionLocal

    db = SessionLocal()
    try:
        row = db.query(ModelEndpoint).filter(ModelEndpoint.id == endpoint_id).first()
        base = (row.base_url if row else "") or ""
    finally:
        db.close()
    if not base:
        return False
    code, body = _request("GET", "/models", url=base.rstrip("/"), timeout=3.0)
    if code != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return False
    ids = {m.get("id") for m in body["data"] if isinstance(m, dict)}
    return bool(ids) and (not model or model in ids)


def deploy(recipe: str, action: str, *, stop_conflicts: bool = False) -> Dict[str, Any]:
    if action not in ("start", "stop"):
        return {"ok": False, "error": "unknown action"}
    name = "deploy_start" if action == "start" else "deploy_stop"
    args: Dict[str, Any] = {"recipe": recipe}
    if action == "start":
        args["stop_conflicts"] = bool(stop_conflicts)
    out = _call(name, args, timeout=900 if action == "stop" else 60)
    try:
        sync()
    except Exception:  # noqa: BLE001
        logger.debug("sparks: sync after %s failed", action, exc_info=True)
    return out


def set_preferences(*, enabled: Optional[bool] = None, url: Optional[str] = None, default_backend: Optional[bool] = None,
                    recipe: Optional[str] = None, first_token_timeout_s: Optional[float] = None) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    if enabled is not None:
        patch["sparks_enabled"] = bool(enabled)
    if url is not None:
        url = url.strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "The address must start with http:// or https://"}
        patch["sparks_url"] = url or DEFAULT_URL
    if default_backend is not None:
        patch["sparks_default_backend"] = bool(default_backend)
        if default_backend:
            patch["sparks_applied_default"] = {}
    if recipe is not None:
        patch["sparks_recipe"] = recipe.strip()
    if first_token_timeout_s is not None:
        if not 5 <= first_token_timeout_s <= 120:
            return {"ok": False, "error": "Initial response wait must be between 5 and 120 seconds"}
        patch["sparks_first_token_timeout_s"] = first_token_timeout_s
    if patch:
        _update(patch)
    try:
        result = sync()
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
    return {"ok": True, "config": {k: v for k, v in config().items() if k in ("enabled", "url", "default_backend", "recipe", "local_default", "first_token_timeout_s")},
            "sync": result}


async def sync_loop() -> None:
    """Startup task: keep the endpoints and the default in line with the Sparks."""
    await asyncio.sleep(15)
    while True:
        try:
            if config()["enabled"]:
                await asyncio.to_thread(sync)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sparks sync failed: %s", exc)
        await asyncio.sleep(SYNC_INTERVAL_S)
